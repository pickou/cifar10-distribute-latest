from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from datetime import datetime
import os.path
import time

import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf

import cifar10

FLAGS = tf.app.flags.FLAGS

tf.app.flags.DEFINE_string('job_name', '', 'One of "ps", "worker"')
tf.app.flags.DEFINE_string('ps_hosts', '',
                           """Comma-separated list of hostname:port for the """
                           """parameter server jobs. e.g. """
                           """'machine1:2222,machine2:1111,machine2:2222'""")
tf.app.flags.DEFINE_string('worker_hosts', '',
                           """Comma-separated list of hostname:port for the """
                           """worker jobs. e.g. """
                           """'machine1:2222,machine2:1111,machine2:2222'""")
tf.app.flags.DEFINE_integer('task_id', 0, 'Task ID of the worker/replica running the training.')


tf.app.flags.DEFINE_string('train_dir', '/opt/tmp/cifar10_train',
                           """Directory where to write event logs """
                           """and checkpoint.""")
tf.app.flags.DEFINE_integer('max_steps', 300,
                            """Number of batches to run.""")
tf.app.flags.DEFINE_boolean('log_device_placement', False,
                            """Whether to log device placement.""")

tf.logging.set_verbosity(tf.logging.INFO)

INITIAL_LEARNING_RATE = 0.1       # Initial learning rate.
MOVING_AVERAGE_DECAY = 0.9999     # The decay to use for the moving average.
NUM_EXAMPLES_PER_EPOCH_FOR_TRAIN = 50000
NUM_EPOCHS_PER_DECAY = 350.0      # Epochs after which learning rate decays.
LEARNING_RATE_DECAY_FACTOR = 0.1  # Learning rate decay factor.

def train():
    ps_hosts = FLAGS.ps_hosts.split(',')
    worker_hosts = FLAGS.worker_hosts.split(',')
    print ('PS hosts are: %s' % ps_hosts)
    print ('Worker hosts are: %s' % worker_hosts)

    server = tf.train.Server({'ps': ps_hosts, 'worker': worker_hosts},
                             job_name = FLAGS.job_name,
                             task_index=FLAGS.task_id)

    if FLAGS.job_name == 'ps':
        server.join()

    is_chief = (FLAGS.task_id == 0)
    if is_chief:
        if tf.gfile.Exists(FLAGS.train_dir):
            tf.gfile.DeleteRecursively(FLAGS.train_dir)
        tf.gfile.MakeDirs(FLAGS.train_dir)


    device_setter = tf.train.replica_device_setter(ps_tasks=1)
    with tf.device('/job:worker/task:%d' % FLAGS.task_id):
        with tf.device(device_setter):
            global_step = tf.Variable(0, trainable=False)

            # Get images and labels for CIFAR-10.
            images, labels = cifar10.distorted_inputs()

            # Build a Graph that computes the logits predictions from the
            # inference model.
            logits = cifar10.inference(images)

            # Calculate loss.
            loss = cifar10.loss(logits, labels)

            # Need to re-implement the training part for sync updates.
            num_batches_per_epoch = NUM_EXAMPLES_PER_EPOCH_FOR_TRAIN / FLAGS.batch_size
            decay_steps = int(num_batches_per_epoch * NUM_EPOCHS_PER_DECAY)

            # Decay the learning rate exponentially based on the number of steps.
            lr = tf.train.exponential_decay(INITIAL_LEARNING_RATE,
                                            global_step,
                                            decay_steps,
                                            LEARNING_RATE_DECAY_FACTOR,
                                            staircase=True)
            tf.summary.scalar('learning_rate', lr)
            opt = tf.train.GradientDescentOptimizer(lr)

            # Track the moving averages of all trainable variables.
            exp_moving_averager = tf.train.ExponentialMovingAverage(MOVING_AVERAGE_DECAY, global_step)
            variables_to_average = (tf.trainable_variables() + tf.moving_average_variables())

            opt = tf.train.SyncReplicasOptimizer(
                opt,
                replicas_to_aggregate=len(worker_hosts),
                total_num_replicas=len(worker_hosts),
                variable_averages=exp_moving_averager,
                variables_to_average=variables_to_average)


            # Compute gradients with respect to the loss.
            grads = opt.compute_gradients(loss)

            # Add histograms for gradients.
            for grad, var in grads:
                if grad is not None:
                    tf.summary.histogram(var.op.name + '/gradients', grad)

            apply_gradients_op = opt.apply_gradients(grads, global_step=global_step)

            with tf.control_dependencies([apply_gradients_op]):
                train_op = tf.identity(loss, name='train_op')


            chief_queue_runners = [opt.get_chief_queue_runner()]
            init_tokens_op = opt.get_init_tokens_op()

            saver = tf.train.Saver()
            # We run the summaries in the same thread as the training operations by
            # passing in None for summary_op to avoid a summary_thread being started.
            # Running summaries and training operations in parallel could run out of
            # GPU memory.
            sv = tf.train.Supervisor(is_chief=is_chief,
                                     logdir=FLAGS.train_dir,
                                     init_op=tf.initialize_all_variables(),
                                     summary_op=tf.summary.merge_all(),
                                     global_step=global_step,
                                     saver=saver,
                                     save_model_secs=60)

            tf.logging.info('%s Supervisor' % datetime.now())

            sess_config = tf.ConfigProto(allow_soft_placement=True,
                                         log_device_placement=FLAGS.log_device_placement)

            print ("Before session init")
            # Get a session.
            sess = sv.prepare_or_wait_for_session(server.target, config=sess_config)
            print ("Session init done")

            # Start the queue runners.
            queue_runners = tf.get_collection(tf.GraphKeys.QUEUE_RUNNERS)
            sv.start_queue_runners(sess, queue_runners)
            print ('Started %d queues for processing input data.' % len(queue_runners))

            sv.start_queue_runners(sess, chief_queue_runners)
            sess.run(init_tokens_op)

            print ('Start training')
            """Train CIFAR-10 for a number of steps."""
            for step in xrange(FLAGS.max_steps):
                start_time = time.time()
                _, loss_value, gs = sess.run([train_op, loss, global_step])
                duration = time.time() - start_time

                assert not np.isnan(loss_value), 'Model diverged with loss = NaN'

                if step % 10 == 0:
                    num_examples_per_step = FLAGS.batch_size
                    examples_per_sec = num_examples_per_step / duration
                    sec_per_batch = float(duration)

                    format_str = ('%s: step %d (global_step %d), loss = %.2f (%.1f examples/sec; %.3f sec/batch)')
                    print (format_str % (datetime.now(), step, gs, loss_value, examples_per_sec, sec_per_batch))

    if is_chief:
        saver.save(sess, os.path.join(FLAGS.train_dir, 'model.ckpt'),
                   global_step=global_step)

def main(argv=None):
    cifar10.maybe_download_and_extract()
    train()

if __name__ == '__main__':
    tf.app.run()
