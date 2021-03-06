"""

Refs:
https://github.com/deepmind/sonnet/blob/master/sonnet/examples/vqvae_example.ipynb
https://github.com/deepmind/sonnet/blob/master/sonnet/python/modules/nets/vqvae.py
https://github.com/rstudio/keras/blob/master/vignettes/examples/vq_vae.R
https://blogs.rstudio.com/tensorflow/posts/2019-01-24-vq-vae/
"""
import time
import random as rn

import numpy as np
import tensorflow as tf
from tensorflow import keras

from tensorflow.python.training import moving_averages

# def main():
random_seed = 42
tf.random.set_seed(random_seed)
np.random.seed(random_seed)
rn.seed(random_seed)

(x_train, y_train), (x_test, y_test) = tf.keras.datasets.mnist.load_data()

x_train = x_train.astype('float32') / 255.
x_test = x_test.astype('float32') / 255.

x_train = x_train.reshape(x_train.shape[0], 28, 28, 1)
x_test = x_test.reshape(x_test.shape[0], 28, 28, 1)

batch_size = 256
train_buf = 60000

train_dataset = tf.data.Dataset.from_tensor_slices((x_train))
train_dataset = train_dataset.shuffle(buffer_size=train_buf)
train_dataset = train_dataset.batch(batch_size)

learning_rate = 0.001
latent_size = 1
num_codes = 123  # K num_embeddings
code_size = 16  # D embedding_dim
base_depth = 32
beta = 0.25
decay = 0.99
input_shape = (28, 28, 1)

# ---------------------------------------------------------------------------------------------------------------
inputs = keras.layers.Input(shape=input_shape)
x = keras.layers.Conv2D(filters=base_depth, kernel_size=5, padding='same', activation='elu')(inputs)
x = keras.layers.Conv2D(filters=base_depth, kernel_size=5, strides=2, padding='same', activation='elu')(x)
x = keras.layers.Conv2D(filters=2 * base_depth, kernel_size=5, padding='same', activation='elu')(x)
x = keras.layers.Conv2D(filters=2 * base_depth, kernel_size=5, strides=2, padding='same', activation='elu')(x)
x = keras.layers.Conv2D(filters=4 * latent_size, kernel_size=7, padding='valid', activation='elu')(x)
x = keras.layers.Flatten()(x)
x = keras.layers.Dense(units=latent_size * code_size)(x)
x = keras.layers.Reshape((latent_size, code_size))(x)
encoder = tf.keras.Model(inputs=inputs, outputs=x)

# ---------------------------------------------------------------------------------------------------------------
inputs = keras.layers.Input(shape=(latent_size, code_size))
x = keras.layers.Reshape((1, latent_size, code_size))(inputs)
x = keras.layers.Conv2DTranspose(filters=2 * base_depth, kernel_size=7, padding='valid', activation='elu')(x)
x = keras.layers.Conv2DTranspose(filters=2 * base_depth, kernel_size=5, padding='same', activation='elu')(x)
x = keras.layers.Conv2DTranspose(filters=2 * base_depth, kernel_size=5, strides=2, padding='same',
                                 activation='elu')(x)
x = keras.layers.Conv2DTranspose(filters=base_depth, kernel_size=5, padding='same', activation='elu')(x)
x = keras.layers.Conv2DTranspose(filters=base_depth, kernel_size=5, strides=2, padding='same', activation='elu')(x)
x = keras.layers.Conv2DTranspose(filters=base_depth, kernel_size=5, padding='same', activation='elu')(x)
x = keras.layers.Conv2D(filters=1, kernel_size=5, padding='same', activation='linear')(x)
decoder = tf.keras.Model(inputs=inputs, outputs=x)

# Vector quantizer -------------------------------------------------------------------

initializer = tf.initializers.GlorotUniform()
init = tf.initializers.Constant(0.0)
codebook = tf.Variable(initializer(shape=(num_codes, code_size)), dtype=tf.float32)
ema_count = tf.Variable(init(shape=(num_codes)), trainable=False) # _ema_cluster_size
ema_means = tf.Variable(codebook.read_value(), trainable=False) # _ema_w



def vector_quantizer(z_e):
    '''Vector Quantization.
    Args:
      z_e: encoded variable. [B, t, D].
    Returns:
      z_q (nearest_codebook_entries) (quantized): nearest embeddings. [B, t, D].
    '''


    # flat_inputs = tf.reshape(inputs, [-1, code_size])
    z = tf.expand_dims(z_e, axis=-2)  # output_shape: (batch_size,latent_size,1,code_size)
    codebook_ = tf.reshape(codebook, (1, 1, num_codes, code_size))  # output_shape: (1,1,num_codes, code_size)
    distances = tf.norm(z - codebook_, axis=-1)  # output_shape:(batch_size, latent_size, num_codes)
    assignments = tf.argmin(distances, axis=2)  # output_shape: k.size (batch_size, latent_size)
    one_hot_assignments = tf.one_hot(assignments, depth=num_codes)
    nearest_codebook_entries = tf.reduce_sum(
        tf.expand_dims(one_hot_assignments, -1) * tf.reshape(codebook, (1, 1, num_codes, code_size)), axis=2)

    return nearest_codebook_entries, one_hot_assignments

def update_ema(one_hot_assignments, codes, decay):
    """

    :param one_hot_assignments: encodings
    :param codes: flat_inputs
    :param decay:
    :return:
    """
    updated_ema_count = moving_averages.assign_moving_average(ema_count,
                                                              tf.reduce_sum(one_hot_assignments, axis=(0,1)),
                                                              decay)
    dw2 = tf.reduce_sum(tf.expand_dims(codes, 2) * tf.expand_dims(one_hot_assignments,3), axis=(0, 1))
    # dw = tf.matmul(flat_inputs, encodings, transpose_a=True)
    updated_ema_means = moving_averages.assign_moving_average(ema_means, dw2, decay)


    # Add small value to avoid dividing by zero
    updated_ema_count = updated_ema_count + 1e-5
    updated_ema_means = updated_ema_means / tf.expand_dims(updated_ema_count, axis=-1)

    codebook.assign(updated_ema_means)

optimizer = tf.keras.optimizers.Adam(lr=learning_rate)

@tf.function
def train_step(x):

    with tf.GradientTape(persistent=True) as ae_tape:
        codes = encoder(x, training=True)
        nearest_codebook_entries, one_hot_assignments = vector_quantizer(codes)
        codes_straight_through = codes + tf.stop_gradient(nearest_codebook_entries - codes) #TRUE QUANTIZED
        x_recon = decoder(codes_straight_through, training=True)

        avg_probs = tf.reduce_mean(one_hot_assignments, 0)
        perplexity = tf.exp(- tf.reduce_sum(avg_probs * tf.math.log(avg_probs + 1e-10)))
        # Losses
        reconstruction_loss = tf.reduce_mean((x_recon - x) ** 2)

        commitment_loss = tf.reduce_mean((tf.stop_gradient(nearest_codebook_entries) - codes) ** 2)

        loss = reconstruction_loss + beta * commitment_loss

    ae_grads = ae_tape.gradient(loss, encoder.trainable_variables + decoder.trainable_variables)
    optimizer.apply_gradients(zip(ae_grads, encoder.trainable_variables + decoder.trainable_variables))


    return loss, one_hot_assignments, codes

epochs = 100
for epoch in range(epochs):
    start = time.time()

    total_loss = 0
    reconstruction_loss_total = 0
    commitment_loss_total = 0
    prior_loss_total = 0

    total_loss = 0.0
    num_batches = 0
    for x in train_dataset:
        loss, one_hot_assignments, codes = train_step(x)
        update_ema(one_hot_assignments, codes, decay)
        total_loss += loss

        num_batches += 1
    train_loss = total_loss / num_batches

    epoch_time = time.time() - start

    template = ("{:4d}: TIME: {:.2f} ETA: {:.2f} AE_LOSS: {:.4f} ")
    print(template.format(epoch+1,
                          epoch_time, epoch_time * (epochs - epoch),
                          train_loss))


