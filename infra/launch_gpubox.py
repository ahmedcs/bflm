#!/usr/bin/env python
#
# Launch a single GPU instance with jupyter notebook

import argparse
import os
import ncluster

parser = argparse.ArgumentParser()
parser.add_argument('--name', type=str, default='gpubox',
                    help="instance name")
parser.add_argument('--image_name', type=str, default='cybertronai01')
parser.add_argument('--conda_env', type=str, default='pytorch_p36')
parser.add_argument('--instance_type', type=str, default='p3.2xlarge',
                    help="type of instance")
parser.add_argument('--password', default='DefaultNotebookPasswordPleaseChange', help='password to use for jupyter notebook')

args = parser.parse_args()
module_path = os.path.dirname(os.path.abspath(__file__))

ncluster.set_backend('aws')

# To enable table of contents plugin
#
# source activate pytorch_p36
# conda install -c conda-forge jupyter_nbextensions_configurator -y
# conda install -c conda-forge jupyter_contrib_nbextensions -y
# conda install ipyparallel -y
# jupyter nbextension enable toc2/main


def main():
  task = ncluster.make_task(name=args.name,
                            instance_type=args.instance_type,
                            disk_size=1000,
                            image_name=args.image_name)

  # upload notebook config with provided password
  jupyter_config_fn = _create_jupyter_config(args.password)
  remote_config_fn = '~/.jupyter/jupyter_notebook_config.py'
  task.run(f'source activate {args.conda_env}')
  task.upload(jupyter_config_fn, remote_config_fn)

  task.run('conda install -c conda-forge jupyter_nbextensions_configurator jupyter_contrib_nbextensions -y ')
  task.run('jupyter nbextension enable toc2/main')

  # upload sample notebook and start Jupyter server
  task.run('mkdir -p /ncluster/notebooks')
  task.upload(f'{module_path}/gpubox_sample.ipynb',
              '/ncluster/notebooks/gpubox_sample.ipynb',
              dont_overwrite=True)
  task.run('cd /ncluster/notebooks')
  task.run('jupyter notebook', non_blocking=True)
  print(f'Jupyter notebook will be at http://{task.public_ip}:8888')


def _create_jupyter_config(password):
  from notebook.auth import passwd
  sha = passwd(args.password)
  local_config_fn = f'{module_path}/gpubox_jupyter_notebook_config.py'
  temp_config_fn = '/tmp/' + os.path.basename(local_config_fn)
  os.system(f'cp {local_config_fn} {temp_config_fn}')
  _replace_lines(temp_config_fn, 'c.NotebookApp.password',
                 f"c.NotebookApp.password = '{sha}'")
  return temp_config_fn


def _replace_lines(fn, startswith, new_line):
  """Replace lines starting with starts_with in fn with new_line."""
  new_lines = []
  for line in open(fn):
    if line.startswith(startswith):
      new_lines.append(new_line)
    else:
      new_lines.append(line)
  with open(fn, 'w') as f:
    f.write('\n'.join(new_lines))


if __name__ == '__main__':
  main()
