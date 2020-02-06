import numpy as np
import tensorflow as tf

from realsafe.attack.base import Attack


class NES(Attack):
    """
    Natural Evolution Strategies (NES)
    A black-box constraint-based method. Use NES as gradient estimation technique and employ PGD with this estimated
    gradient to generate the adversarial example.

    Supported distance metric: `l_2`, `l_inf`
    Supported goal: `t`, `tm`, `ut`
    Supported config parameters:
    - `magnitude`: max distortion, could be either a float number or a numpy float number array with shape of
        (batch_size,).

    References:
    [1] https://arxiv.org/abs/1804.08598
    [2] http://www.jmlr.org/papers/volume15/wierstra14a/wierstra14a.pdf
    """

    def __init__(self, model, goal, distance_metric, session, samples_per_draw):
        self.model, self._session = model, session
        self.goal, self.distance_metric = goal, distance_metric
        self.samples_per_draw = (samples_per_draw // 2) * 2

        self.x_var = tf.Variable(tf.zeros(dtype=self.model.x_dtype, shape=self.model.x_shape))
        self.x_adv_var = tf.Variable(tf.zeros(dtype=self.model.x_dtype, shape=self.model.x_shape))
        self.ys_var = tf.Variable(tf.zeros(dtype=self.model.y_dtype, shape=self.samples_per_draw))
        self.eps_var = tf.Variable(0.0, dtype=self.model.x_dtype)
        self.sigma_var = tf.Variable(0.0, dtype=tf.float32)
        self.lr_var = tf.Variable(0.0, dtype=tf.float32)

        self.x_ph = tf.placeholder(model.x_dtype, self.model.x_shape)
        self.ys_ph = tf.placeholder(model.y_dtype, (self.samples_per_draw,))
        self.eps_ph = tf.placeholder(self.model.x_dtype)
        self.sigma_ph = tf.placeholder(dtype=tf.float32)
        self.lr_ph = tf.placeholder(dtype=tf.float32)

        self.label_pred = self.model.logits_and_labels(tf.reshape(self.x_adv_var, (1, *self.model.x_shape)))[1][0]
        # pertubations for each step
        perts = tf.random.normal(shape=(self.samples_per_draw // 2, *self.model.x_shape), dtype=self.model.x_dtype)
        perts = tf.concat([perts, -perts], axis=0)
        # points to eval the logits
        points = self.x_adv_var + self.sigma_var * perts
        logits, _ = self.model.logits_and_labels(points)
        # NES uses margin logit loss proposed in C&W.
        logits_mask = tf.one_hot(self.ys_var, self.model.n_class)
        logit_this = tf.reduce_sum(logits_mask * logits, axis=-1)
        logit_that = tf.reduce_max(logits - 99999 * logits_mask, axis=-1)
        self.loss = logit_that - logit_this
        # estimated gradient
        grads = tf.reshape(self.loss, [-1] + [1] * len(self.model.x_shape)) * perts
        grad = tf.reduce_mean(grads, axis=0) / self.sigma_var
        if self.goal != 'ut':
            grad = -grad
        # update the adversarial example
        if self.distance_metric == 'l_2':
            grad_norm = tf.maximum(1e-12, tf.norm(grad))
            x_adv_delta = self.x_adv_var - self.x_var + self.lr_var * grad / grad_norm
            x_adv_next = self.x_var + tf.clip_by_norm(x_adv_delta, self.eps_var)
        elif self.distance_metric == 'l_inf':
            x_adv_delta = self.x_adv_var - self.x_var + self.lr_var * tf.sign(grad)
            x_adv_next = self.x_var + tf.clip_by_value(x_adv_delta, -self.eps_var, self.eps_var)
        else:
            raise NotImplementedError
        x_adv_next = tf.clip_by_value(x_adv_next, self.model.x_min, self.model.x_max)
        self.update_x_adv_step = self.x_adv_var.assign(x_adv_next)

        self.config_eps_step = self.eps_var.assign(self.eps_ph)
        self.config_sigma_step = self.sigma_var.assign(self.sigma_ph)
        self.config_lr_step = self.lr_var.assign(self.lr_ph)

        self.setup_x_step = [self.x_var.assign(self.x_ph), self.x_adv_var.assign(self.x_ph)]
        self.setup_ys_step = self.ys_var.assign(self.ys_ph)

        self.logger = None
        self.details = {}

    def config(self, **kwargs):
        if 'magnitude' in kwargs:
            self._session.run(self.config_eps_step, feed_dict={self.eps_ph: kwargs['magnitude']})
        if 'max_queries' in kwargs:
            self.max_queries = kwargs['max_queries']
        if 'sigma' in kwargs:
            self._session.run(self.config_sigma_step, feed_dict={self.sigma_ph: kwargs['sigma']})
        if 'lr' in kwargs:
            self.lr = kwargs['lr']
        if 'min_lr' in kwargs:
            self.min_lr = kwargs['min_lr']
        if 'lr_tuning' in kwargs:
            self.lr_tuning = kwargs['lr_tuning']
        if 'plateau_length' in kwargs:
            self.plateau_length = kwargs['plateau_length']
        if 'logger' in kwargs:
            self.logger = kwargs['logger']

    def attack(self, x, y=None, y_target=None):
        self._session.run(self.setup_x_step, feed_dict={self.x_ph: x})
        self._session.run(self.setup_ys_step, feed_dict={
            self.ys_ph: np.repeat(y if self.goal == 'ut' else y_target, self.samples_per_draw)
        })

        last_loss = []
        queries = 0
        lr = self.lr
        self._session.run(self.config_lr_step, feed_dict={self.lr_ph: lr})

        if self._is_adversarial(y, y_target):
            if self.logger:
                self.logger.info('Original image is already adversarial')
            self.details['queries'] = queries
            return x

        while queries < self.max_queries:
            loss, _ = self._session.run((self.loss, self.update_x_adv_step))
            queries += self.samples_per_draw

            if self.lr_tuning:
                last_loss.append(np.mean(loss))
                last_loss = last_loss[-self.plateau_length:]
                if len(last_loss) == self.plateau_length:
                    if self.goal == 'ut' and last_loss[-1] < last_loss[0]:
                        lr = max(lr / 2, self.min_lr)
                        self._session.run(self.config_lr_step, feed_dict={self.lr_ph: lr})
                        last_loss = []
                    elif self.goal != "ut" and last_loss[-1] > last_loss[0]:
                        lr = max(lr / 2, self.min_lr)
                        self._session.run(self.config_lr_step, feed_dict={self.lr_ph: lr})
                        last_loss = []

            if self.logger:
                x_adv_label, x_adv = self._session.run((self.label_pred, self.x_adv_var))
                self.logger.info("queries:{}, loss:{}, learning rate:{}, prediction:{}, distortion:{} {}".format(
                    queries, np.mean(loss), lr, x_adv_label, np.max(np.abs(x_adv - x)), np.linalg.norm(x_adv - x)
                ))

            if self._is_adversarial(y, y_target):
                self.details['queries'] = queries
                return self._session.run(self.x_adv_var)

        self.details['queries'] = queries
        return self._session.run(self.x_adv_var)

    def _is_adversarial(self, y, y_target):
        # label of x_adv
        label = self._session.run(self.label_pred)
        if self.goal == 'ut' or self.goal == 'tm':
            return label != y
        else:
            return label == y_target
