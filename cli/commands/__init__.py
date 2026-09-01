"""
GCO CLI command groups.

Each module defines a Click command group that is registered
on the root ``cli`` group via ``cli.add_command()``.
"""

from .analytics_cmd import analytics
from .autopilot_cmd import autopilot
from .capacity_cmd import capacity
from .cluster_cmd import cluster
from .config_cmd import config_cmd
from .costs_cmd import costs
from .dag_cmd import dag
from .deps_cmd import deps
from .examples_cmd import examples
from .files_cmd import files
from .images_cmd import images
from .inference_cmd import inference
from .jobs_cmd import jobs
from .mission_cmd import mission_cmd
from .models_cmd import models
from .monitoring_cmd import monitoring
from .nodepools_cmd import nodepools
from .queue_cmd import queue
from .release_cmd import release
from .stacks_cmd import stacks
from .status_cmd import status
from .storage_cmd import storage
from .swarm_cmd import swarm_cmd
from .tasks_cmd import tasks
from .templates_cmd import templates
from .vector_cmd import vector
from .webhooks_cmd import webhooks

__all__ = [
    "analytics",
    "autopilot",
    "capacity",
    "cluster",
    "config_cmd",
    "costs",
    "dag",
    "deps",
    "examples",
    "files",
    "images",
    "inference",
    "jobs",
    "mission_cmd",
    "swarm_cmd",
    "models",
    "monitoring",
    "nodepools",
    "queue",
    "release",
    "stacks",
    "status",
    "storage",
    "tasks",
    "templates",
    "vector",
    "webhooks",
]
