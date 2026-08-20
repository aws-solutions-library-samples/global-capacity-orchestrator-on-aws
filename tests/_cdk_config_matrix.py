"""Shared ``cdk.json`` configuration matrix.

Single source of truth for ``tests/test_nag_compliance.py`` and
``tests/test_cdk_synthesis_matrix.py``. Both need to iterate over
the same set of cdk.json overlays to catch the same
configuration-specific regressions — divergence between the two lists
is how we ended up with an ``AwsSolutions-IAM5`` error on a
``gco-us-east-1`` deploy that neither tool had ever exercised. Keep
this list as the canonical definition; both sides just import
``CONFIGS`` from here.

Each entry is a ``(name, overrides)`` tuple where ``overrides`` is a
shallow dict merged into the baseline ``cdk.json`` context before the
CDK app is constructed. Dict values are merged per-key (not
replaced), so a partial override like
``{"eks_cluster": {"endpoint_access": "PUBLIC_AND_PRIVATE"}}`` leaves
other keys in the ``eks_cluster`` block alone.

Notes on the list
-----------------

* ``default-regions`` is always first; it mirrors whatever cdk.json
  ships with the repo. The synthesis matrix script used to rely on
  that ordering to establish a baseline before applying overrides.

* The ``helm-*`` entries exercise the helm chart enable/disable
  matrix. These matter for the compliance test because the Helm
  installer Lambda's IAM role changes shape based on which charts
  it has to install.
"""

from __future__ import annotations

from typing import Any

CONFIGS: list[tuple[str, dict[str, Any]]] = [
    ("default-regions", {}),
    (
        "us-west-regions",
        {
            "deployment_regions": {
                "global": "us-west-2",
                "api_gateway": "us-west-2",
                "monitoring": "us-west-2",
                "regional": ["us-west-1"],
            }
        },
    ),
    (
        "eu-regions",
        {
            "deployment_regions": {
                "global": "eu-west-1",
                "api_gateway": "eu-west-1",
                "monitoring": "eu-west-1",
                "regional": ["eu-central-1"],
            }
        },
    ),
    (
        "multi-region",
        {
            "deployment_regions": {
                "global": "us-east-2",
                "api_gateway": "us-east-2",
                "monitoring": "us-east-2",
                "regional": ["us-east-1", "us-west-2"],
            }
        },
    ),
    (
        # Single-region topology: every stack (global, api_gateway,
        # monitoring) collapses onto the one regional entry. This is the
        # exact shape from issue #125 — when the API Gateway stack is
        # co-located with the regional stack, the auth-secret cross-stack
        # reference resolves to a native CloudFormation export
        # (``gco-api-gateway:ExportsOutputRefGCOAuthSecret<hash>``) instead of
        # a literal ARN, which used to slip past the AwsSolutions-IAM5
        # suppression and fail ``cdk synth``. Every other topology in this
        # matrix keeps ``regional`` in a different region from ``global`` /
        # ``api_gateway``, so this is the only entry that exercises the
        # same-region auth-secret code path. Keep it here as a regression
        # guard for both the synthesis matrix and the cdk-nag matrix.
        "single-region",
        {
            "deployment_regions": {
                "global": "us-east-1",
                "api_gateway": "us-east-1",
                "monitoring": "us-east-1",
                "regional": ["us-east-1"],
            }
        },
    ),
    (
        "valkey-enabled",
        {
            "valkey": {
                "enabled": True,
                "max_data_storage_gb": 5,
                "max_ecpu_per_second": 5000,
                "snapshot_retention_limit": 1,
            }
        },
    ),
    (
        "valkey-disabled",
        {
            "valkey": {
                "enabled": False,
                "max_data_storage_gb": 5,
                "max_ecpu_per_second": 5000,
                "snapshot_retention_limit": 1,
            }
        },
    ),
    (
        "fsx-enabled",
        {
            "fsx_lustre": {
                "enabled": True,
                "storage_capacity_gib": 1200,
                "deployment_type": "SCRATCH_2",
                "file_system_type_version": "2.15",
                "per_unit_storage_throughput": 200,
                "data_compression_type": "LZ4",
                "import_path": None,
                "export_path": None,
                "auto_import_policy": "NEW_CHANGED_DELETED",
            }
        },
    ),
    ("fsx-disabled", {"fsx_lustre": {"enabled": False}}),
    ("endpoint-private", {"eks_cluster": {"endpoint_access": "PRIVATE"}}),
    ("endpoint-public-private", {"eks_cluster": {"endpoint_access": "PUBLIC_AND_PRIVATE"}}),
    (
        "aurora-pgvector-enabled",
        {
            "aurora_pgvector": {
                "enabled": True,
                "min_acu": 0,
                "max_acu": 16,
                "backup_retention_days": 7,
                "deletion_protection": False,
            }
        },
    ),
]

CONFIGS.extend(
    [
        (
            "thresholds-all-disabled",
            {
                "resource_thresholds": {
                    "cpu_threshold": -1,
                    "memory_threshold": -1,
                    "gpu_threshold": -1,
                    "pending_pods_threshold": -1,
                    "pending_requested_cpu_vcpus": -1,
                    "pending_requested_memory_gb": -1,
                    "pending_requested_gpus": -1,
                }
            },
        ),
        (
            "thresholds-aggressive",
            {
                "resource_thresholds": {
                    "cpu_threshold": 90,
                    "memory_threshold": 90,
                    "gpu_threshold": 95,
                    "pending_pods_threshold": 50,
                    "pending_requested_cpu_vcpus": 500,
                    "pending_requested_memory_gb": 1000,
                    "pending_requested_gpus": 100,
                }
            },
        ),
        (
            "all-features-enabled",
            {
                "valkey": {
                    "enabled": True,
                    "max_data_storage_gb": 10,
                    "max_ecpu_per_second": 10000,
                    "snapshot_retention_limit": 3,
                },
                "fsx_lustre": {
                    "enabled": True,
                    "storage_capacity_gib": 2400,
                    "deployment_type": "SCRATCH_2",
                    "file_system_type_version": "2.15",
                    "per_unit_storage_throughput": 200,
                    "data_compression_type": "LZ4",
                    "import_path": None,
                    "export_path": None,
                    "auto_import_policy": "NEW_CHANGED_DELETED",
                },
                "aurora_pgvector": {
                    "enabled": True,
                    "min_acu": 0,
                    "max_acu": 16,
                    "backup_retention_days": 7,
                    "deletion_protection": False,
                },
                "eks_cluster": {"endpoint_access": "PUBLIC_AND_PRIVATE"},
            },
        ),
        (
            "minimal-config",
            {
                "valkey": {
                    "enabled": False,
                    "max_data_storage_gb": 5,
                    "max_ecpu_per_second": 5000,
                    "snapshot_retention_limit": 1,
                },
                "fsx_lustre": {"enabled": False},
                "eks_cluster": {"endpoint_access": "PRIVATE"},
            },
        ),
        (
            "ap-regions",
            {
                "deployment_regions": {
                    "global": "ap-southeast-1",
                    "api_gateway": "ap-southeast-1",
                    "monitoring": "ap-southeast-1",
                    "regional": ["ap-northeast-1"],
                }
            },
        ),
        (
            "three-regions",
            {
                "deployment_regions": {
                    "global": "us-east-2",
                    "api_gateway": "us-east-2",
                    "monitoring": "us-east-2",
                    "regional": ["us-east-1", "eu-west-1", "ap-northeast-1"],
                }
            },
        ),
        (
            "valkey-large",
            {
                "valkey": {
                    "enabled": True,
                    "max_data_storage_gb": 100,
                    "max_ecpu_per_second": 50000,
                    "snapshot_retention_limit": 7,
                }
            },
        ),
        (
            "fsx-with-s3-import",
            {
                "fsx_lustre": {
                    "enabled": True,
                    "storage_capacity_gib": 1200,
                    "deployment_type": "PERSISTENT_2",
                    "file_system_type_version": "2.15",
                    "per_unit_storage_throughput": 500,
                    "data_compression_type": "LZ4",
                    "import_path": "s3://my-bucket/data",
                    "export_path": "s3://my-bucket/output",
                    "auto_import_policy": "NEW_CHANGED_DELETED",
                }
            },
        ),
        (
            "high-api-limits",
            {
                "api_gateway": {
                    "throttle_rate_limit": 10000,
                    "throttle_burst_limit": 20000,
                    "log_level": "ERROR",
                    "metrics_enabled": True,
                    "tracing_enabled": False,
                }
            },
        ),
        (
            "helm-slurm-yunikorn-enabled",
            {
                "helm": {
                    "slurm": {"enabled": True},
                    "yunikorn": {"enabled": True},
                }
            },
        ),
        (
            "helm-minimal",
            {
                "helm": {
                    "keda": {"enabled": False},
                    "volcano": {"enabled": False},
                    "kuberay": {"enabled": False},
                    "aws_efa_device_plugin": {"enabled": False},
                }
            },
        ),
        (
            "helm-gpu-only",
            {
                "helm": {
                    "keda": {"enabled": False},
                    "volcano": {"enabled": False},
                    "kuberay": {"enabled": False},
                    "kueue": {"enabled": False},
                    "cert_manager": {"enabled": False},
                    "aws_efa_device_plugin": {"enabled": False},
                    "aws_neuron_device_plugin": {"enabled": False},
                }
            },
        ),
        (
            "helm-all-schedulers",
            {
                "helm": {
                    "slurm": {"enabled": True},
                    "yunikorn": {"enabled": True},
                    "volcano": {"enabled": True},
                    "kueue": {"enabled": True},
                    "kuberay": {"enabled": True},
                }
            },
        ),
        (
            "analytics-enabled",
            {
                "analytics_environment": {
                    "enabled": True,
                    "hyperpod": {"enabled": False},
                }
            },
        ),
        (
            "analytics-enabled-hyperpod",
            {
                "analytics_environment": {
                    "enabled": True,
                    "hyperpod": {"enabled": True},
                }
            },
        ),
        (
            "analytics-enabled-canvas",
            {
                "analytics_environment": {
                    "enabled": True,
                    "hyperpod": {"enabled": False},
                    "canvas": {"enabled": True},
                }
            },
        ),
        (
            "analytics-enabled-hyperpod-canvas",
            {
                "analytics_environment": {
                    "enabled": True,
                    "hyperpod": {"enabled": True},
                    "canvas": {"enabled": True},
                }
            },
        ),
        (
            "analytics-efs-retain",
            {
                "analytics_environment": {
                    "enabled": True,
                    "hyperpod": {"enabled": False},
                    "efs": {"removal_policy": "retain"},
                    "cognito": {"removal_policy": "destroy"},
                }
            },
        ),
        (
            "analytics-cognito-retain",
            {
                "analytics_environment": {
                    "enabled": True,
                    "hyperpod": {"enabled": False},
                    "efs": {"removal_policy": "destroy"},
                    "cognito": {"removal_policy": "retain"},
                }
            },
        ),
        (
            "analytics-custom-domain-prefix",
            {
                "analytics_environment": {
                    "enabled": True,
                    "hyperpod": {"enabled": False},
                    "cognito": {
                        "removal_policy": "destroy",
                        "domain_prefix": "my-custom-studio",
                    },
                    "efs": {"removal_policy": "destroy"},
                }
            },
        ),
        (
            "analytics-all-retain",
            {
                "analytics_environment": {
                    "enabled": True,
                    "hyperpod": {"enabled": True},
                    "efs": {"removal_policy": "retain"},
                    "cognito": {"removal_policy": "retain"},
                }
            },
        ),
        # Mission memory ships ON by default, so ``default-regions`` (and every
        # other entry) already synthesizes the table + vector-index custom
        # resource; the disabled overlay is the compatibility contract — an
        # operator opting out must synthesize the pre-feature global stack.
        (
            "mission-memory-disabled",
            {"mission_memory": {"enabled": False}},
        ),
    ]
)

# Volcano image mirror (gco/stacks/regional_stack.py
# _configure_volcano_image_mirror). Enabling it injects the Volcano
# basic.image_registry override into the HelmInstallCharts custom resource and
# creates no new resources, so the nag matrix exercises that synth path.
CONFIGS.append(
    (
        "volcano-mirror-enabled",
        {
            "volcano_image_mirror": {
                "enabled": True,
                "ecr_namespace": "gco/dockerhub",
            }
        },
    )
)

# ---------------------------------------------------------------------------
# Coverage sweep — every remaining ConfigLoader context surface.
# ---------------------------------------------------------------------------
# tests/test_cdk_synthesis_matrix.py::test_every_config_knob_is_matrix_covered
# asserts that every context key the loader reads is varied by at least one
# entry (or sits on MATRIX_COVERAGE_ALLOWLIST with a justification). These
# entries close the gap that guard found on first run: the Global
# Accelerator block (traffic dial, health-check contract, legacy-key
# tolerance), the Historical Capacity Surface, and several long-standing
# blind spots that had never synthesized off their defaults.
CONFIGS.extend(
    [
        # Traffic-dial controller, monitor mode (the shipped default when
        # enabled): controller Lambda + EventBridge schedule + DLQ
        # synthesize through the real ConfigLoader merge path.
        (
            "ga-traffic-dial-monitor",
            {"global_accelerator": {"traffic_dial": {"enabled": True}}},
        ),
        # Enforce mode is the only shape granted
        # globalaccelerator:UpdateEndpointGroup — a distinct IAM surface
        # (also in NAG_CONFIGS). Knob edges ride along: a 7-minute cadence
        # and a zero dial floor.
        (
            "ga-traffic-dial-enforce",
            {
                "global_accelerator": {
                    "traffic_dial": {
                        "enabled": True,
                        "mode": "enforce",
                        "interval_minutes": 7,
                        "min_dial_percentage": 0,
                    }
                }
            },
        ),
        # Every non-dial Global Accelerator knob at a non-default value:
        # the 10-second probe interval (the only other value the GA API
        # accepts), a tuned threshold, a custom health path, SOURCE_IP
        # affinity, and a custom accelerator name.
        (
            "ga-health-check-tuned",
            {
                "global_accelerator": {
                    "name": "gco-matrix-accelerator",
                    "health_check_interval": 10,
                    "health_check_threshold": 5,
                    "health_check_path": "/healthz",
                    "client_affinity": "SOURCE_IP",
                }
            },
        ),
        # Legacy configs carrying the never-consumed
        # health_check_grace_period / health_check_timeout keys must still
        # synthesize: the keys were removed from the schema but are
        # documented as tolerated, so an existing cdk.json upgrading in
        # place cannot fail synth.
        (
            "ga-legacy-keys-tolerated",
            {
                "global_accelerator": {
                    "health_check_grace_period": 300,
                    "health_check_timeout": 10,
                }
            },
        ),
        # Historical Capacity Surface off: the shipped cdk.json enables it,
        # so the poller-absent shape (no table, no schedule, no SSM param)
        # only synthesizes here.
        ("historical-disabled", {"historical": {"enabled": False}}),
        # Historical tuned: long-block probe disabled (0 hours), a single
        # spot-score target capacity, an explicit region subset, and a
        # trimmed watch list — env-serialization branches the defaults
        # never exercise.
        (
            "historical-tuned",
            {
                "historical": {
                    "enabled": True,
                    "retention_days": 30,
                    "poll_interval_minutes": 60,
                    "capacity_block_duration_hours": 48,
                    "capacity_block_long_duration_hours": 0,
                    "spot_score_target_capacities": [1],
                    "watch_instance_types": [
                        "g5.xlarge",
                        "p5.48xlarge",
                        "trn2.48xlarge",
                    ],
                    "enabled_regions": ["us-east-1"],
                }
            },
        ),
        # The vector store defaults OFF, so its entire optional surface
        # (global table + vector index + S3-triggered ingest + regional
        # read wiring) synthesizes only through this entry.
        (
            "vector-store-enabled",
            {"vector_store": {"enabled": True, "replica_regions": ["us-east-1"]}},
        ),
        # Backend TLS lifecycle knobs at non-defaults that still satisfy
        # every cross-field validator (leaf < root validity, overlap >
        # leaf validity, activation delay > max stale trust window).
        (
            "backend-tls-tuned",
            {
                "backend_tls": {
                    "leaf_validity_days": 14,
                    "leaf_rotate_before_days": 7,
                    "rotation_schedule_hours": 6,
                    "trust_cache_ttl_seconds": 120,
                    "trust_cache_max_stale_seconds": 1800,
                }
            },
        ),
        # Observability and cost monitoring off together (cost reporting
        # reads OpenCost, so the coherent disable is both at once).
        (
            "observability-and-cost-disabled",
            {
                "cluster_observability": {"enabled": False},
                "cost_monitoring": {"enabled": False},
            },
        ),
        # Cost monitoring kept on but tuned: report cadence, retention,
        # and the infrequent-access transition age.
        (
            "cost-monitoring-tuned",
            {
                "cost_monitoring": {
                    "reports": {
                        "interval_minutes": 120,
                        "retention_days": 90,
                        "transition_to_infrequent_access_days": 30,
                    }
                }
            },
        ),
        # Grafana/Prometheus persistence sizing flows through to Helm values.
        (
            "observability-tuned",
            {
                "cluster_observability": {
                    "grafana": {"persistence_size": "20Gi"},
                    "prometheus": {"persistence_size": "100Gi", "retention": "30d"},
                }
            },
        ),
        # Regional ALB probe contract at non-defaults.
        (
            "alb-tuned",
            {
                "alb_config": {
                    "health_check_interval": 15,
                    "health_check_timeout": 10,
                    "healthy_threshold": 3,
                    "unhealthy_threshold": 4,
                }
            },
        ),
        # FSx enabled for a subset of a multi-region topology:
        # fsx_lustre_regions gates which regional stacks grow the FSx
        # surface, and only a multi-region deployment exercises the subset.
        (
            "fsx-region-subset",
            {
                "deployment_regions": {
                    "global": "us-east-2",
                    "api_gateway": "us-east-2",
                    "monitoring": "us-east-2",
                    "regional": ["us-east-1", "us-west-2"],
                },
                "fsx_lustre": {"enabled": True},
                "fsx_lustre_regions": ["us-east-1"],
            },
        ),
        # Non-default project name: #139 made every physical name derive
        # from this single knob, so one full-app synth under a different
        # prefix guards the derivation end to end (the dedicated scoping
        # test asserts the details; this proves the whole graph builds).
        # Custom tags ride along — they stamp every taggable resource.
        (
            "custom-project-and-tags",
            {
                "project_name": "gcoalt",
                "tags": {
                    "Project": "GCOALT",
                    "Environment": "staging",
                    "CostCenter": "ml-1234",
                },
            },
        ),
        # Manifest-processor service shape (replica count + request-body
        # cap) and a tightened job-validation policy flow into the regional
        # stack's container env and the queue processor.
        (
            "manifest-processor-tuned",
            {
                "manifest_processor": {
                    "replicas": 5,
                    "max_request_body_bytes": 524288,
                },
                "job_validation_policy": {
                    "resource_quotas": {"max_gpu_per_manifest": 8},
                },
            },
        ),
    ]
)

# Context keys the loader reads that the matrix deliberately does NOT vary.
# Every entry needs a justification; the coverage guard fails if an
# allowlisted key gains matrix coverage (stale allowlist) or a read key is
# neither covered nor listed.
MATRIX_COVERAGE_ALLOWLIST: dict[str, str] = {
    "kubernetes_version": (
        "Pinned platform version: valid values are coupled to the aws-cdk-lib "
        "release and the EKS standard-support window, both owned by the "
        "monthly dependency scan and the documented upgrade runbook — a "
        "matrix entry would hardcode a second copy of that moving target."
    ),
}


# ---------------------------------------------------------------------------
# NAG_CONFIGS — subset of CONFIGS used by tests/test_nag_compliance.py
# ---------------------------------------------------------------------------
# The full CONFIGS list is used by tests/test_cdk_synthesis_matrix.py
# (serial in-process synth validation; shared CDK asset staging races under
# pytest-xdist). For the in-process cdk-nag compliance test, we only need the
# *distinct IAM policy surfaces*.
# Most configs (valkey-disabled, thresholds-aggressive, helm-minimal, etc.)
# change Helm charts, resource quotas, or threshold values that don't touch
# IAM at all — running cdk-nag on them is pure overhead.
#
# The configs below cover every IAM code path:
#   1. default-regions     — baseline single-region, all standard roles
#   2. multi-region        — cross-region SSM/DynamoDB roles, 2 regional stacks
#   3. fsx-enabled         — FSx CSI IRSA role + PassRole on shared CR role
#   4. all-features-enabled — FSx + Valkey + public endpoint combined
#   5. three-regions       — 3 regional stacks, max cross-region surface
#
# Plus two analytics-environment fixtures that exercise the
# optional ``GCOAnalyticsStack`` IAM surface — SageMaker Studio execution
# role, Cognito user pool, EMR Serverless application, and the presigned-
# URL Lambda role. These two configs are the only ones where
# ``config.get_analytics_enabled()`` returns ``True``, so the test
# harness ``_build_all_stacks`` has to instantiate the analytics stack
# for them.

_NAG_CONFIG_NAMES = {
    "default-regions",
    "multi-region",
    # Single-region topology (global == api_gateway == regional). This is the
    # IAM code path from issue #125: the auth-secret grant on the regional
    # service-account role must stay covered by its AwsSolutions-IAM5
    # suppression even when the API Gateway stack is co-located with the
    # regional stack. It is the only config that exercises the same-region
    # cross-stack reference form, so it MUST run through cdk-nag, not just the
    # synthesis matrix.
    "single-region",
    "fsx-enabled",
    "all-features-enabled",
    "three-regions",
    "aurora-pgvector-enabled",
    # Exercises the Volcano image-mirror registry override (no new resources,
    # just the basic.image_registry override on the HelmInstallCharts CR).
    "volcano-mirror-enabled",
    "analytics-enabled",
    # ``analytics-enabled-hyperpod-canvas`` exercises *both* analytics
    # sub-toggles in a single synth — it subsumes the coverage that
    # dedicated ``analytics-enabled-hyperpod`` and
    # ``analytics-enabled-canvas`` entries would give us individually.
    # Keeping only the combined variant in the nag matrix avoids two
    # redundant full-app synths without losing IAM surface coverage,
    # because both sub-toggles layer on top of the baseline
    # ``analytics-enabled`` IAM surface independently.
    "analytics-enabled-hyperpod-canvas",
    # The traffic-dial controller's IAM role is new in v6.4: enforce mode is
    # its maximal shape (adds globalaccelerator:UpdateEndpointGroup to the
    # wildcard-resource GA statement) and carries every suppression the
    # monitor shape uses (namespace-conditioned PutMetricData, project-scoped
    # SSM tree, DLQ without its own redrive) — so the single enforce entry
    # covers the whole controller surface under cdk-nag.
    "ga-traffic-dial-enforce",
}

NAG_CONFIGS: list[tuple[str, dict[str, Any]]] = [
    (name, overrides) for name, overrides in CONFIGS if name in _NAG_CONFIG_NAMES
]

# Sanity check — if someone renames a config in CONFIGS but forgets to
# update _NAG_CONFIG_NAMES, this catches it at import time rather than
# silently running fewer configs.
assert len(NAG_CONFIGS) == len(_NAG_CONFIG_NAMES), (
    f"NAG_CONFIGS has {len(NAG_CONFIGS)} entries but expected "
    f"{len(_NAG_CONFIG_NAMES)}. Check that _NAG_CONFIG_NAMES matches "
    f"the names in CONFIGS: missing = "
    f"{_NAG_CONFIG_NAMES - {n for n, _ in NAG_CONFIGS}}"
)
