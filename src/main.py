import datetime
import json
import pickle
import random
from pathlib import Path

import cv2
import numpy as np
import os
# Run TF on CPU
# os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
import tensorflow as tf

from tensorflow.keras.preprocessing.image import ImageDataGenerator
from tensorflow.keras.optimizers import Adam
from tensorflow.keras import Input
from tensorflow.keras.callbacks import TensorBoard
from tensorflow.keras.layers import Conv2D, Conv2DTranspose, Add
from tensorflow.keras.models import Model
from tensorflow.keras.callbacks import ReduceLROnPlateau, EarlyStopping, ModelCheckpoint
from tensorflow.keras.optimizers.schedules import PolynomialDecay
from tensorflow.keras.callbacks import LearningRateScheduler

from utils.eval import avg_metric, avg_metric_loaded_array
from utils.dataset import load_cifar, blur_cifar, reshape_cifar, unpickle

# Avoids memory overflow
tf.config.experimental.set_memory_growth(tf.config.list_physical_devices('GPU')[0], True)

# Use the Tensor Cores on the GPU
# policy = tf.keras.mixed_precision.experimental.Policy('mixed_float16')
# mixed_precision.set_policy(policy)

# Paths to the datasets
cifar_path = '../res/datasets/cifar-10/'
cifar_path_modified = cifar_path + 'modified/'
cifar_train_path = cifar_path_modified+'data_batch_unified'
cifar_test_path = cifar_path_modified+'test_batch'
cifar_blurred_train_path = cifar_path_modified+'data_batch_unified_blurred'
cifar_blurred_test_path = cifar_path_modified+'test_batch_blurred'
cifar_saved_paths = {'train': cifar_path+'saved/train/original/', 'train_b': cifar_path+'saved/train/blurred/',
                     'test': cifar_path+'saved/test/original/', 'test_b': cifar_path+'saved/test/blurred/'}

reds_path = '../res/datasets/REDS/'
reds_train_sharp = reds_path + 'train/train_sharp/'
reds_train_blur = reds_path + 'train/train_blur/'
reds_val_sharp = reds_path + 'val/val_sharp/'
reds_val_blur = reds_path + 'val/val_blur/'
reds_test_blur = reds_path + 'test/test_blur/'

# Path to the parameters used to execute
json_path = 'params.json'  # TODO1 set up argparse

# Define some parameters/hyper-parameters
seed = 42
batch_size = 8
class_mode = None
epochs = 50
initial_lr = 1e-4
# Number of image channels, number of scale levels, starting scale
channels = 3
n_levels = 3
starting_scale = 0.5
# Model checkpoint period
mc_period = 1
# Callbacks parameters
end_lr = 1e-6
power = 0.3
monitor_rlrop = 'val_loss'
factor_rlrop = 0.2
patience_rlrop = 5
min_lr_rlrop = 1e-5
monitor_es = 'loss'
patience_es = 3

rescale = 1./255
validation_split = 0.1
# Channel last
input_shape = (None, None, 3)
random_crop_size = (256, 256)

# Load parameters
with open(json_path) as json_file:
    data = json.load(json_file)

    task = data["task"]
    epochs = data['epochs']
    batch_size = data['batch_size']
    seed = data['seed']
    load_epoch = data['load_epoch']
    initial_lr = data['initial_lr']
    mc_period = data['mc_period']
    subset = data['subset']
    action = data['action']

# minor_path can be 'reds' or 'cifar'; it's used to create the paths where to save things (e.g. logs, ...)
task_path = 'reds' if 'reds' in task else 'cifar'

# Instantiate a TensorBoard callback and the path where to save the logs
base_logs_path = '../res/logs/'
log_dir = base_logs_path+task_path+'/'+task_path + datetime.datetime.now().strftime('%Y%m%d-%H%M%S')

# Path where to save the checkpoints
base_model_path = '../res/models/'+task_path
checkpoint_filepath = base_model_path+'/checkpoints/model.{epoch:04d}-{val_loss:.4f}.h5'

# Path where to save/load the model/weights
model_weights_path = base_model_path+'/model-'+str(load_epoch)+'.h5'
final_model_path = base_model_path

# Path where to save predictions
out_reds = '../res/datasets/REDS/out/val/'
out_cifar = '../res/datasets/cifar-10/saved/out/test/folder/'

# Different target size depending on the task to perform (work on 'reds' or 'cifar' dataset)
if 'reds' in task:
    target_size = (720, 1280)
else:
    target_size = (32, 32)

# Work on a subset of train and validation sets for quick tests (reds only)
if 'reds' in task:
    if subset:
        reds_train_sharp = reds_path + 'train_s/train_sharp/'
        reds_train_blur = reds_path + 'train_s/train_blur/'
        reds_val_sharp = reds_path + 'val_s/val_sharp/'
        reds_val_blur = reds_path + 'val_s/val_blur/'
        reds_test_blur = reds_path + 'test/test_blur/'

# Set the seed
random.seed(seed)
np.random.seed(seed)
tf.random.set_seed(seed)

# If the cifar dataset does not exists, create it: load, reshape and blur the datasets
if not os.path.exists(cifar_train_path):
    dataset = load_cifar(cifar_path)
    ds_blurred = blur_cifar(dataset)

    for k in ['train', 'test']:
        reshaped_ds = reshape_cifar(dataset[k])
        reshaped_ds_b = reshape_cifar(ds_blurred[k])

        path_c = cifar_train_path if k == 'train' else cifar_test_path
        path_c_b = cifar_blurred_train_path if k == 'train' else cifar_blurred_test_path

        Path(cifar_path_modified).mkdir(parents=True, exist_ok=True)

        # Save the unpdated datasets
        with open(path_c, 'wb') as ff:
            pickle.dump(reshaped_ds, ff)
        with open(path_c_b, 'wb') as ff:
            pickle.dump(reshaped_ds_b, ff)

# Those are data (n_images, 32, 32, 3)
cifar = {'train': unpickle(cifar_train_path), 'test': unpickle(cifar_test_path),
         'train_b': unpickle(cifar_blurred_train_path), 'test_b': unpickle(cifar_blurred_test_path)}

'''
# Save CIFAR-10 images
for key in cifar_saved_paths:
    Path(cifar_saved_paths[key]).mkdir(parents=True, exist_ok=True)
    save_cifar(cifar[key], cifar_saved_paths[key])
'''

# Those are paths
reds = {'train_s': reds_train_sharp, 'train_b': reds_train_blur, 'val_s': reds_val_sharp, 'val_b': reds_val_blur,
        'test_b': reds_test_blur}

# reds = keras_folder(reds)

# for key in reds:
#     reds_merge(reds[k])

# Create the datagens
train_datagen = ImageDataGenerator(rescale=rescale, validation_split=validation_split)
test_datagen = ImageDataGenerator(rescale=rescale)

# Create the generators. Need a train_generator for both the sharp and blur images, which will be combined with
# a combined_generator
if 'reds' in task:
    train_sharp_generator = train_datagen.flow_from_directory(
            reds['train_s'],
            target_size=target_size,
            batch_size=batch_size, class_mode=class_mode, seed=seed, subset='training')
    train_blur_generator = train_datagen.flow_from_directory(
            reds['train_b'],
            target_size=target_size,
            batch_size=batch_size, class_mode=class_mode, seed=seed, subset='training')

    val_sharp_generator = train_datagen.flow_from_directory(
            reds['train_s'],
            target_size=target_size,
            batch_size=batch_size, class_mode=class_mode, seed=seed, subset='validation')
    val_blur_generator = train_datagen.flow_from_directory(
            reds['train_b'],
            target_size=target_size,
            batch_size=batch_size, class_mode=class_mode, seed=seed, subset='validation')

    # Validation generator used for testing
    test_val_sharp_generator = train_datagen.flow_from_directory(
            reds['val_s'],
            target_size=target_size,
            batch_size=1, class_mode=class_mode, seed=seed, shuffle=False)
    test_val_blur_generator = train_datagen.flow_from_directory(
            reds['val_b'],
            target_size=target_size,
            batch_size=1, class_mode=class_mode, seed=seed, shuffle=False)

    test_generator = test_datagen.flow_from_directory(
            reds['test_b'],
            target_size=target_size,
            batch_size=batch_size, class_mode=class_mode, seed=seed)
else:
    train_sharp_generator = train_datagen.flow(
            x=cifar['train'],
            y=None,
            batch_size=batch_size, seed=seed, subset='training')
    train_blur_generator = train_datagen.flow(
        x=cifar['train_b'],
        y=None,
        batch_size=batch_size, seed=seed, subset='training')

    val_sharp_generator = train_datagen.flow(
        x=cifar['train'],
        y=None,
        batch_size=batch_size, seed=seed, subset='validation')
    val_blur_generator = train_datagen.flow(
        x=cifar['train_b'],
        y=None,
        batch_size=batch_size, seed=seed, subset='validation')

    test_sharp_generator = test_datagen.flow(
        x=cifar['test'],
        y=None,
        batch_size=batch_size, seed=seed, shuffle=False)
    test_blur_generator = test_datagen.flow(
        x=cifar['test_b'],
        y=None,
        batch_size=batch_size, seed=seed, shuffle=False)


def random_crop(sharp_batch, blur_batch):
    """
    Random crops the sharp and blur patch, with the random_crop_size dimension.

    :param sharp_batch (np.array): batch of sharp images
    :param blur_batch (np.array): batch of blur images
    :return: cropped (Tuple[np.array, np.array]): cropped batch
    """
    s = []
    b = []

    for image_s, image_b in zip(sharp_batch, blur_batch):
        height, width = image_s.shape[0], image_s.shape[1]
        dy, dx = random_crop_size
        x = np.random.randint(0, width - dx + 1)
        y = np.random.randint(0, height - dy + 1)
        s.append(image_s[y:(y + dy), x:(x + dx), :])
        b.append(image_b[y:(y + dy), x:(x + dx), :])

    return np.array(s), np.array(b)


def combine_generators(sharp_generator, blur_generator):
    """
    Yields batches of sharp and blur images, cropped.

    :param sharp_generator (DataFrameIterator): Keras DataFrameIterator of sharp images
    :param blur_generator (DataFrameIterator): Keras DataFrameIterator of blur images
    :return: batches (Tuple[np.array, np.array]): batches of sharp and blur images
    """
    while True:
        sharp_batch = sharp_generator.next()
        blur_batch = blur_generator.next()

        sharp_batch, blur_batch = random_crop(sharp_batch, blur_batch)

        res = [sharp_batch, blur_batch]

        yield res


def combine_generators_no_random_crop(sharp_generator, blur_generator):
    """
    Yields batches of sharp and blur images.

    :param sharp_generator (DataFrameIterator): Keras DataFrameIterator of sharp images
    :param blur_generator (DataFrameIterator): Keras DataFrameIterator of blur images
    :return: batches (Tuple[np.array, np.array]): batches of sharp and blur images
    """
    while True:
        sharp_batch = sharp_generator.next()
        blur_batch = blur_generator.next()

        res = [sharp_batch, blur_batch]

        yield res


# Create the generators, without crops for the cifar task (and the reds test_val)
if 'reds' in task:
    train_generator = combine_generators(train_sharp_generator, train_blur_generator)
    validation_generator = combine_generators(val_sharp_generator, val_blur_generator)
    test_val_generator = combine_generators_no_random_crop(test_val_sharp_generator, test_val_blur_generator)
else:
    train_generator = combine_generators_no_random_crop(train_sharp_generator, train_blur_generator)
    validation_generator = combine_generators_no_random_crop(val_sharp_generator, val_blur_generator)
    test_generator = combine_generators_no_random_crop(test_sharp_generator, test_blur_generator)

# Define the train and validation steps
if 'reds' in task:
    train_steps = train_sharp_generator.samples // batch_size
    validation_steps = val_sharp_generator.samples // batch_size
else:
    train_steps = len(train_sharp_generator)
    validation_steps = len(val_sharp_generator)


# Define the NN model
def res_net_block(x, filters, ksize):
    """
    Define a ResNet block (Conv2D -> Cond2D).

    :param x (tf.keras.Model): Keras model on which the block will be appended (Functional API)
    :param filters (int): Number of filters
    :param ksize (int): Kernel size
    :return: y (tf.keras.Model): Updated model
    """
    net = Conv2D(filters=filters, kernel_size=(ksize, ksize), padding='same', activation='relu')(x)
    net = Conv2D(filters=filters, kernel_size=(ksize, ksize), padding='same', activation=None)(net)

    return net


# def generator(inp):
def generator(inp, x_unwrap=[]):
    """
    Define the generator model. See relation for deeper explanation of this part of the code.

    :param inp (tf.keras.layers.Layer): Input of the NN
    :param x_unwrap (list): List of the logical scales (see relation)
    :return: inp_pred (tf.keras.layers.Layer): last layer of the network (see relation)
    """

    # If training on reds, the shape is (256, 256) (crop)
    # If predicting on reds, the shape is the original one (720, 1280)
    # If training/predicting on cifar, the shape is the original one (32, 32)
    if action == 0:
        if 'reds' in task:
            h, w = random_crop_size
        else:
            h, w = target_size[0], target_size[1]
    else:
        h, w = target_size[0], target_size[1]

    # x_unwrap = []
    inp_pred = inp
    # Iterate over the number of levels
    for i in range(n_levels):
        # Compute the scale to resize the h and w of the image
        scale = starting_scale ** (n_levels - i - 1)
        hi = int(round((h*scale)))
        wi = int(round((w*scale)))

        # Resize the blurred and prediction images
        inp_blur = tf.image.resize(inp, [hi, wi])
        inp_pred = tf.image.resize(inp_pred, [hi, wi])
        inp_all = tf.concat([inp_blur, inp_pred], axis=3, name='inp')

        # Encoder
        conv1_1 = Conv2D(filters=32, kernel_size=(5, 5), padding='same', activation='relu')(inp_all)
        conv1_2 = res_net_block(conv1_1, 32, 5)
        conv1_3 = res_net_block(conv1_2, 32, 5)
        conv1_4 = res_net_block(conv1_3, 32, 5)

        conv2_1 = Conv2D(filters=64, kernel_size=(5, 5), strides=2, padding='same',
                         activation='relu')(conv1_4)
        conv2_2 = res_net_block(conv2_1, 64, 5)
        conv2_3 = res_net_block(conv2_2, 64, 5)
        conv2_4 = res_net_block(conv2_3, 64, 5)

        conv3_1 = Conv2D(filters=128, kernel_size=(5, 5), strides=2, padding='same',
                         activation='relu')(conv2_4)
        conv3_2 = res_net_block(conv3_1, 128, 5)
        conv3_3 = res_net_block(conv3_2, 128, 5)
        conv3_4 = res_net_block(conv3_3, 128, 5)

        # Decoder
        deconv3_4 = conv3_4
        deconv3_3 = res_net_block(deconv3_4, 128, 5)
        deconv3_2 = res_net_block(deconv3_3, 128, 5)
        deconv3_1 = res_net_block(deconv3_2, 128, 5)

        deconv2_4 = Conv2DTranspose(filters=64, kernel_size=(4, 4), strides=2, padding='same',
                                    activation='relu')(deconv3_1)
        # Skip connection (cat2, cat1)
        cat2 = Add()([deconv2_4, conv2_4])
        deconv2_3 = res_net_block(cat2, 64, 5)
        deconv2_2 = res_net_block(deconv2_3, 64, 5)
        deconv2_1 = res_net_block(deconv2_2, 64, 5)

        deconv1_4 = Conv2DTranspose(filters=32, kernel_size=(4, 4), strides=2, padding='same',
                                    activation='relu')(deconv2_1)
        cat1 = Add()([deconv1_4, conv1_4])
        deconv1_3 = res_net_block(cat1, 32, 5)
        deconv1_2 = res_net_block(deconv1_3, 32, 5)
        deconv1_1 = res_net_block(deconv1_2, 32, 5)

        inp_pred = Conv2D(filters=channels, kernel_size=(5, 5), padding='same', activation=None)(deconv1_1)

        if i >= 0:
            x_unwrap.append(inp_pred)

    # return x_unwrap
    return inp_pred


def custom_loss(x_unwrap, img_gt):
    """
    Loss of the NN. See relation.

    :param x_unwrap (list): List of the logical scales (see relation)
    :param img_gt (tf.keras.layers.Layer): GT input (sharp images)
    :return: loss (float): loss value
    """

    loss_total = 0
    for i in range(n_levels):
        batch_s, hi, wi, channels = x_unwrap[i].get_shape().as_list()
        gt_i = tf.image.resize(img_gt, [hi, wi])
        loss = tf.reduce_mean((gt_i - x_unwrap[i]) ** 2)
        loss_total += loss

    return loss_total


# Define some metrics
def log10(x):
    """
    Compute the log10 (instead of ln) of a tf.Tensor.

    :param x (tf.Tensor): input
    :return log10 (tf.Tensor): output
    """
    numerator = tf.math.log(x)
    denominator = tf.math.log(tf.constant(10, dtype=numerator.dtype))
    return numerator / denominator


def custom_psnr(x_unwrap, input_sharp, last_level=False):
    """
    PSNR metric (between predicted and sharp image).

    :param x_unwrap (list): List of the logical scales (see relation)
    :param input_sharp (tf.keras.layers.Layer): GT input (sharp images)
    :param last_level (boolean): True if the psnr is computed only for the last level;
        False for averaging over the 3 levels
    :return: psnr (float): psnr for the current batch (for each image averaged over the 3 (or 1) levels)
    """
    n_levels = 3

    metric_total = 0
    for i in range(n_levels):
        batch_s, hi, wi, channels = x_unwrap[i].get_shape().as_list()
        gt_i = tf.image.resize(input_sharp, [hi, wi])
        metric = 20*log10((1.0 ** 2) / tf.math.sqrt(tf.reduce_mean((gt_i - x_unwrap[i]) ** 2)))
        metric_total += metric

    metric_total /= 3

    if last_level:
        return metric

    return metric_total


# Define the 2 inputs of the NN (sharp and blur)
input_sharp = Input(shape=input_shape, name='input_sharp')
input_blur = Input(shape=input_shape, name='input_blur')

x_unwrap = []
# Define the output (prediction of deblurred)
output = generator(input_blur, x_unwrap)
# Define the model
model = Model(inputs=[input_sharp, input_blur], outputs=output)

# x_unwrap = generator(input_blur)
# model = Model(inputs=[input_sharp, input_blur], outputs=x_unwrap)

# Add custom loss and metric
model.add_loss(custom_loss(x_unwrap, input_sharp))
# Since training happens on batch of images we will use the mean of SSIM values of all the images in the batch as the
# loss value -> batch_mean(mean_scales_mse)
model.add_metric(custom_psnr(x_unwrap, input_sharp), name='mean_scales_psnr', aggregation='mean')
# Compile the model
OPTIMIZER = Adam(lr=initial_lr)
model.compile(optimizer=OPTIMIZER)

# Print the summary
print(model.summary())

# Callbacks
tensorboard_callback = TensorBoard(log_dir=log_dir)  # , histogram_freq=1, profile_batch='1')

save_weights_only = False

# PolynomialDecay definition
if 'reds' in task:
    data_size = train_sharp_generator.samples // batch_size
else:
    data_size = len(train_sharp_generator)
max_steps = int(epochs * data_size)

pd = PolynomialDecay(initial_learning_rate=initial_lr, decay_steps=max_steps, end_learning_rate=end_lr, power=power)

rlrop = ReduceLROnPlateau(monitor=monitor_rlrop, factor=factor_rlrop, patience=patience_rlrop, min_lr=min_lr_rlrop)
lrs = LearningRateScheduler(pd)
es = EarlyStopping(monitor=monitor_es, patience=patience_es)
mc = ModelCheckpoint(filepath=checkpoint_filepath, monitor='val_loss', save_best_only=False,
                     save_weights_only=save_weights_only, period=mc_period)

callbacks = [tensorboard_callback, mc]

if 'reds' in task:
    callbacks.append(lrs)
else:
    callbacks.append(rlrop)

# Check if tf is using GPU
# print('Using GPU: {}'.format(tf.test.is_gpu_available()))
print(tf.config.list_physical_devices('GPU'))

# Restart the training from a model (weights) or load a model (weights) to make predictions
if load_epoch != 0:
    model.load_weights(model_weights_path)
    # model = load_model(model_weights_path)
    print('Loaded model/weights!')

if action == 0:  # Train action
    # Train
    history = model.fit(train_generator, epochs=epochs, steps_per_epoch=train_steps, callbacks=callbacks,
                        validation_data=validation_generator, validation_steps=validation_steps,
                        initial_epoch=load_epoch)

    # Save the model/weights
    model.save(final_model_path+'/final_model.h5')
    model.save_weights(final_model_path+'/final_weights.h5')
    print('Saved model/weights!')
else:  # Predict/evaluate # TODO1 do function
    if 'reds' in task:
        names = test_val_sharp_generator.filenames
        names = iter(names)

        # Path where to save predicted images
        out = out_reds

        if action == 1:  # Predict
            Path(out+'folder/').mkdir(parents=True, exist_ok=True)

            count = 0
            for batch in test_val_generator:
                # Make prediction
                p = model(batch)
                imguint8 = np.squeeze(p.numpy()*255, axis=0)
                # Save image
                cv2.imwrite(out+next(names), cv2.cvtColor(imguint8, cv2.COLOR_RGB2BGR))
                count += 1
                print('Predicted {}/{}'.format(count, len(test_val_sharp_generator.filenames)))

        if action == 2:  # Evaluate
            original_path = reds_val_sharp+'folder/'
            deblurred_path = out+'folder/'
            # Compute metrics
            a_m, a_p, a_s = avg_metric(original_path, deblurred_path)
            print('Avg. MSE, PSNR, SSIM: {:.2f}, {:.2f}, {:.2f}'.format(a_m, a_p, a_s))
    else:
        # Path where to save the images
        out = out_cifar

        if action >= 1:
            Path(out).mkdir(parents=True, exist_ok=True)
            sharp = []
            blur = []
            deblur = []

            count = 0
            for batch in test_generator:
                # Make prediction
                p = model(batch)  # TODO1 do multiprocessing, if possible
                imguint8 = p.numpy()*255
                sharp.extend(batch[0]*255)
                blur.extend(batch[1]*255)
                deblur.extend(imguint8)

                if False:  # Save the images
                    for i in range(len(imguint8)):
                        cv2.imwrite(out+str(count+i)+'.png', imguint8[i])

                count += len(imguint8)
                print('Predicted {}/10000'.format(count))

                if count >= 10000:  # Infinite generator
                    break

            sharp = np.array(sharp)
            blur = np.array(blur)
            deblur = np.array(deblur)

            # Compute metrics
            a_m, a_p, a_s = avg_metric_loaded_array(sharp, deblur)
            print('Avg. MSE, PSNR, SSIM: {:.2f}, {:.2f}, {:.2f}'.format(a_m, a_p, a_s))
