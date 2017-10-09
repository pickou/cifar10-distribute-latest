#!/usr/bin/env python
# coding=utf-8
import subprocess

cmds = ["python cifar10_sync_dist_train.py --ps_hosts localhost:2220 --job_name ps --worker_hosts localhost:2221,localhost:2222 --task_id 0",
       "python cifar10_sync_dist_train.py --ps_hosts localhost:2220 --job_name worker --worker_hosts localhost:2221,localhost:2222 --task_id 0",
       "python cifar10_sync_dist_train.py --ps_hosts localhost:2220 --job_name worker --worker_hosts localhost:2221,localhost:2222 --task_id 1"
      ]

if __name__=="__main__":
    for cmd in cmds:
        subprocess.Popen(cmd, shell=True)
