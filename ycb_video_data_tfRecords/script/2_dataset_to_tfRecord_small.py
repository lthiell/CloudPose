import os
import tensorflow as tf
import numpy as np
import random
import open3d
import sys
from datetime import datetime

NUM_CLASS = 1
NUM_POINT = 1024
BATCH_SIZE = 1
minimum_points_in_segment = NUM_POINT
threshold_distance_per_class = 0.2 * np.ones((NUM_CLASS,), dtype=np.float32)

data_dir = '../../ycb_style_mini_tfrecords/0000/'
out_dir = '../../ycb_style_mini_tfrecords/FPS1024/'

# all train sequences for each object class
seq_id = [
# target_cls = 0
[0],
]


def decode(x):
    features = tf.parse_single_example(
        x,
        features={
            'image': tf.FixedLenFeature((), tf.string),
            'image_shape': tf.FixedLenFeature((3,), tf.int64),
            'depth': tf.FixedLenFeature((), tf.string),
            'depth_shape': tf.FixedLenFeature((2,), tf.int64),
            'label': tf.FixedLenFeature((), tf.string),
            'label_shape': tf.FixedLenFeature((2,), tf.int64),
            'quaternions': tf.FixedLenFeature([NUM_CLASS, 4], tf.float32),
            'translations': tf.FixedLenFeature([NUM_CLASS, 3], tf.float32),
            'class_one_hot': tf.FixedLenFeature([NUM_CLASS], tf.int64),
            'seq_id': tf.FixedLenFeature([], tf.int64),
            'frame_id': tf.FixedLenFeature([], tf.int64),
            'fx': tf.FixedLenFeature([], tf.float32),
            'fy': tf.FixedLenFeature([], tf.float32),
            'cx': tf.FixedLenFeature([], tf.float32),
            'cy': tf.FixedLenFeature([], tf.float32),
            'factor_depth': tf.FixedLenFeature([], tf.float32),
        })

    image_flat = tf.decode_raw(features["image"], out_type=tf.uint8)
    image = tf.reshape(image_flat, shape=features["image_shape"])

    is_four_channel_image = tf.equal(tf.shape(image)[2], 4)
    image = tf.cond(is_four_channel_image, true_fn=lambda: image[:, :, :3], false_fn=lambda: image)

    features['image'] = image

    depth_flat = tf.decode_raw(features["depth"], out_type=tf.uint16)
    features['depth'] = tf.reshape(depth_flat, shape=features["depth_shape"])

    label_flat = tf.decode_raw(features["label"], out_type=tf.uint8)
    features['label'] = tf.reshape(label_flat, shape=features["label_shape"])

    return features


def get_pointcloud(depth, fx, fy, cx, cy, depth_scaling_factor):
    depth_meters = tf.cast(depth, tf.float32) / depth_scaling_factor

    dshape = tf.shape(depth_meters)
    height = dshape[0]
    width = dshape[1]
    xv = tf.cast(tf.range(width), tf.float32)
    yv = tf.cast(tf.range(height), tf.float32)
    X, Y = tf.meshgrid(xv, yv)

    x = ((X - cx) * depth_meters / fx)
    y = ((Y - cy) * depth_meters / fy)
    xyz = tf.stack([x, y, depth_meters], axis=2)  # (height, width, 3)

    return tf.reshape(xyz, [height * width, 3])


def merge_two_dicts(x, y):
    z = x.copy()  # start with x's keys and values
    z.update(y)  # modifies z with y's keys and values & returns None
    return z


def split_samples(x):
    xyz = get_pointcloud(x["depth"], x["fx"], x["fy"], x["cx"], x["cy"], x["factor_depth"])
    rgb = tf.reshape(tf.image.convert_image_dtype(x['image'], dtype=tf.float32), [-1, 3])
    hsv = tf.image.rgb_to_hsv(rgb)
    yuv = tf.image.rgb_to_yuv(rgb)
    yiq = tf.image.rgb_to_yiq(rgb)

    class_idx = tf.where(x["class_one_hot"])
    classes = tf.reshape(class_idx, [-1])
    quaternions = tf.squeeze(tf.gather(x["quaternions"], class_idx))
    translations = tf.squeeze(tf.gather(x["translations"], class_idx))

    depth_flat = tf.cast(tf.reshape(x["depth"], [-1]), tf.int64)
    depth_valid = tf.not_equal(depth_flat, 0)

    data_static = {'xyz': xyz,
                   'rgb': rgb,
                   'hsv': hsv,
                   'yuv': yuv,
                   'yiq': yiq,
                   'depth_valid': depth_valid,
                   'frame_id': x["frame_id"],
                   'seq_id': x["seq_id"],
                   'label': x["label"],
                   # 'image': x["image"]
                   }
    d_static = tf.data.Dataset.from_tensors(data_static).repeat()

    data_dynamic = {'class_id': classes,
                    'quaternion': quaternions,
                    'translation': translations
                    }
    d_dynamic = tf.data.Dataset.from_tensor_slices(data_dynamic)

    ds = tf.data.Dataset.zip((d_static, d_dynamic))
    ds = ds.map(lambda y, x: merge_two_dicts(y, x))
    return ds


def segment_mean_distance_filter(xyz, label_mask, threshold_distance):
    # Filtering based on distance from mean of segment
    segment_average_xyz = tf.reduce_mean(tf.boolean_mask(xyz, label_mask), axis=0)
    d = tf.norm(xyz-segment_average_xyz, ord='euclidean', axis=1)
    return tf.logical_and(label_mask, tf.less_equal(d, threshold_distance))


def calc_distances(p0, points):
    return ((p0 - points)**2).sum(axis=1)


def FPS_random(pts, K, seq_id, frame_id, class_id):
    farthest_pts = np.zeros((K, 3))
    farthest_pts_idx = np.zeros(K)
    print ("seq %d frame %d class %d segment_size %d" % (seq_id, frame_id, class_id, pts.shape[0]))
    upper_bound = pts.shape[0] - 1
    if upper_bound==0:
        print ("ZERO seq %d frame %d class %d " % (seq_id, frame_id, class_id))
    sys.stdout.flush()
    first_idx = random.randint(0, upper_bound)
    farthest_pts[0] = pts[first_idx]
    farthest_pts_idx[0] = first_idx
    distances = calc_distances(farthest_pts[0, 0:3], pts[:, 0:3])
    for i in range(1, K):
        farthest_pts[i] = pts[np.argmax(distances)]
        farthest_pts_idx[i] = np.argmax(distances)
        distances = np.minimum(distances, calc_distances(farthest_pts[i, 0:3], pts[:, 0:3]))
    return farthest_pts_idx.astype(np.int64)


# label exist for not presenting object
def segment_not_empty(x):
    label_flat = tf.cast(tf.reshape(x["label"], [-1]), tf.int64) - 1  # To zero-based class indexing!
    label_mask = tf.logical_and(tf.equal(label_flat, x["class_id"]), x["depth_valid"])
    label_mask_r = segment_mean_distance_filter(x['xyz'], label_mask,
                                                threshold_distance=tf.gather(threshold_distance_per_class,
                                                                             x["class_id"]))
    x["label_mask_r"] = label_mask_r
    x["segment_after_filter"] = tf.count_nonzero(label_mask_r)
    return x


def sample_segment(x, numpoints, threshold_distance_per_class):
    label_mask_r = x["label_mask_r"]
    num_nonzeros = tf.count_nonzero(label_mask_r)

    idx = tf.py_func(FPS_random, [tf.boolean_mask(x['xyz'], label_mask_r), numpoints, x['seq_id'], x['frame_id'], x['class_id']], tf.int64)

    y_out = {'class_id': x['class_id'],
             'seq_id': x['seq_id'],
             'frame_id': x['frame_id'],
             'quaternion': x['quaternion'],
             'translation': x['translation'],
             }

    y_out["num_valid_points_in_segment"] = num_nonzeros
    y_out["xyz"] = tf.gather(tf.boolean_mask(x['xyz'], label_mask_r), idx)
    y_out["rgb"] = tf.gather(tf.boolean_mask(x['rgb'], label_mask_r), idx)

    return y_out


def create_tfrecord_dataset(filename, num_points_per_sample, minimum_points_in_segment, threshold_distance_per_class, target_cls):
    ds = tf.data.TFRecordDataset(filename)
    ds = ds.map(decode)
    ds = ds.filter(lambda x: tf.equal(x["class_one_hot"][target_cls], 1)) # let frame with target class pass
    ds = ds.flat_map(split_samples)
    ds = ds.map(segment_not_empty)
    ds = ds.filter(lambda x: tf.greater(x["segment_after_filter"], 0))
    ds = ds.filter(lambda x: tf.equal(x["class_id"], target_cls))  # only take target cls segment
    ds = ds.map(lambda x: sample_segment(x, num_points_per_sample, threshold_distance_per_class))
    ds = ds.filter(lambda x: tf.greater_equal(x["num_valid_points_in_segment"], minimum_points_in_segment))
    ds = ds.batch(BATCH_SIZE, drop_remainder=True)
    ds = ds.prefetch(1)
    return ds


def _int64_feature(value):
    """Wrapper for inserting int64 features into Example proto."""
    if not isinstance(value, list):
        value = [value]
    return tf.train.Feature(int64_list=tf.train.Int64List(value=value))


def _float_feature(value):
    # """Wrapper for inserting float features into Example proto."""
    # if not isinstance(value, list):
    #     value = [value]
    return tf.train.Feature(float_list=tf.train.FloatList(value=value))


def _bytes_feature(value):
    # """Wrapper for inserting bytes features into Example proto."""
    # if not isinstance(value, list):
    #     value = [value]
    return tf.train.Feature(bytes_list=tf.train.BytesList(value=value))


def create_example(datasample):
    example = tf.train.Example(features=tf.train.Features(
        feature={
            'class_id': _int64_feature(datasample['class_id'].reshape(-1)),
            'seq_id': _int64_feature(datasample['seq_id'].reshape(-1)),
            'frame_id': _int64_feature(datasample['frame_id'].reshape(-1)),
            'quaternion': _float_feature(datasample['quaternion'].reshape(-1)),
            'translation': _float_feature(datasample['translation'].reshape(-1)),
            'num_valid_points_in_segment': _int64_feature(datasample['num_valid_points_in_segment'].reshape(-1)),
            'xyz': _float_feature(datasample['xyz'].reshape(-1)),
            'rgb': _float_feature(datasample['rgb'].reshape(-1)),
        }
    ))
    return example


def dataset_generator(ds, sess):

    print (ds)
    tr_iterator = ds.make_one_shot_iterator()
    next_element = tr_iterator.get_next()

    try:
        while True:
            yield sess.run(next_element)

    except tf.errors.OutOfRangeError:
        pass


def creat_records(ds, record_path):

    counter = 0

    with tf.device('/cpu:0'):
        sess = tf.Session()

        with tf.python_io.TFRecordWriter(record_path) as writer:

            generator = dataset_generator(ds, sess)

            for datasample in generator:

                print (counter)

                example = create_example(datasample)
                writer.write(example.SerializeToString())
                counter = counter + 1


def get_data_set(target_cls):

    train_file_lists = []

    for i in seq_id[target_cls]:
        filename = str(i).zfill(4) + ".tfrecord"
        train_file_lists.append(filename)
    train_file_lists.append("synthetic.tfrecord")

    train_file_list = [os.path.join(data_dir, f) for f in train_file_lists]

    tr_datasets = [create_tfrecord_dataset(f, NUM_POINT, minimum_points_in_segment,
                                               threshold_distance_per_class, target_cls) for f in train_file_list]
    tr_dataset = tf.data.experimental.sample_from_datasets(tr_datasets)
    record_path = out_dir + 'train_files_FPS1024_' + str(target_cls) + '.tfrecords'
    creat_records(tr_dataset, record_path=record_path)


def main():
    for i in np.arange(0, 1):
        start_time = datetime.now()
        get_data_set(i)
        time_elapsed = datetime.now() - start_time
        print('Time elapsed (hh:mm:ss.ms) {}'.format(time_elapsed))


main()