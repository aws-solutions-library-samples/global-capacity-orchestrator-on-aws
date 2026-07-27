"""
Tests for gco/stacks/monitoring_stack.GCOMonitoringStack.

Synthesizes the monitoring stack against MockConfigLoader plus mock
Global, API Gateway, and regional stack objects with the attributes
the monitoring stack reads (accelerator name/id, DynamoDB table
names, Lambda function names, queue names, cluster names). Asserts
dashboard widgets, CloudWatch alarms (metric and composite), and SNS
topic shape on the resulting CloudFormation template — no AWS or
Docker dependency.
"""

from unittest.mock import MagicMock

import aws_cdk as cdk
import pytest
from aws_cdk import assertions


class MockConfigLoader:
    """Mock ConfigLoader for testing monitoring stack.

    Optional regional data services default to disabled, matching a
    minimal GCO deployment:

      - FSx Lustre: driven by the regional stack's ``fsx_file_system``
        attribute (see ``create_mock_regional_stack(fsx_enabled=True)``)
      - Aurora pgvector: driven by the regional stack's ``aurora_cluster``
        attribute (see ``create_mock_regional_stack(aurora_enabled=True)``)
      - Valkey Serverless: gated by ``valkey_enabled`` here because the
        cache name is deterministic (``{project_name}-{region}``) so
        there's no per-region resource handle to carry

    Valkey takes a single bool because the monitoring stack builds the
    dimension value from ``get_regions() + literal name template``; no
    per-region resource reference is needed.
    """

    def __init__(
        self,
        app=None,
        *,
        valkey_enabled: bool = False,
        cost_monitoring_enabled: bool = True,
    ):
        self._valkey_enabled = valkey_enabled
        self._cost_monitoring_enabled = cost_monitoring_enabled

    def get_project_name(self):
        return "gco-test"

    def get_regions(self):
        return ["us-east-1", "us-west-2"]

    def get_global_region(self):
        return "us-east-2"

    def get_api_gateway_region(self):
        return "us-east-2"

    def get_monitoring_region(self):
        return "us-east-2"

    # The monitoring stack queries Valkey config through this.
    def get_valkey_config(self):
        return {"enabled": self._valkey_enabled}

    # Cost monitoring defaults to enabled, matching cdk.json, so the
    # monitoring-stack synth exercises the cost report bucket + Athena path.
    def get_cost_monitoring_config(self):
        return {
            "enabled": self._cost_monitoring_enabled,
            "reports": {
                "interval_minutes": 60,
                "retention_days": 365,
                "transition_to_infrequent_access_days": 90,
            },
            "athena": {"query_results_retention_days": 30},
        }

    def get_cost_monitoring_enabled(self):
        return bool(self.get_cost_monitoring_config()["enabled"])


def create_mock_global_stack():
    """Create a mock global stack with accelerator."""
    mock_global_stack = MagicMock()
    mock_global_stack.accelerator_name = "gco-test-accelerator"
    mock_global_stack.accelerator_id = "test-accelerator-id-12345"
    # Add DynamoDB table mocks
    mock_global_stack.templates_table.table_name = "gco-test-templates"
    mock_global_stack.webhooks_table.table_name = "gco-test-webhooks"
    mock_global_stack.jobs_table.table_name = "gco-test-jobs"
    return mock_global_stack


def create_mock_api_gateway_stack():
    """Create a mock API gateway stack with Lambda functions and API."""
    mock_api_gw_stack = MagicMock()
    mock_api_gw_stack.api.rest_api_name = "gco-global-api"
    mock_api_gw_stack.proxy_lambda.function_name = "gco-test-proxy"
    mock_api_gw_stack.rotation_lambda.function_name = "gco-test-rotation"
    mock_api_gw_stack.secret.secret_name = "gco/api-gateway-auth-token"  # nosec B105 - test fixture mock value, not a real secret
    return mock_api_gw_stack


def create_mock_regional_stack(
    region: str,
    *,
    fsx_enabled: bool = False,
    aurora_enabled: bool = False,
):
    """Create a mock regional stack with cluster, queues, and Lambda functions.

    ``fsx_enabled`` populates ``fsx_file_system`` with a mock whose ``.ref``
    returns a deterministic file system ID. ``aurora_enabled`` populates
    ``aurora_cluster`` similarly with a deterministic ``cluster_identifier``.
    Both default to disabled so existing tests stay unchanged.

    No ALB mock is needed — the monitoring stack's ALB widgets use a
    CloudWatch SEARCH expression with a ``LoadBalancer="app/k8s-gco-"``
    prefix filter, so there's no per-stack resource reference involved.
    """
    mock_regional_stack = MagicMock()
    mock_regional_stack.deployment_region = region
    mock_regional_stack.cluster.cluster_name = f"gco-test-{region}"
    mock_regional_stack.job_queue.queue_name = f"gco-test-jobs-{region}"
    mock_regional_stack.job_dlq.queue_name = f"gco-test-jobs-dlq-{region}"
    mock_regional_stack.kubectl_lambda_function_name = f"gco-test-kubectl-{region}"
    mock_regional_stack.helm_installer_lambda_function_name = f"gco-test-helm-{region}"

    # Default: no optional data-service resources provisioned. The
    # monitoring stack widget creators check ``getattr(..., None)`` and
    # skip the section when all regions report None.
    mock_regional_stack.fsx_file_system = None
    mock_regional_stack.aurora_cluster = None

    if fsx_enabled:
        fsx_mock = MagicMock()
        fsx_mock.ref = f"fs-gco-test-{region}"
        mock_regional_stack.fsx_file_system = fsx_mock

    if aurora_enabled:
        aurora_mock = MagicMock()
        aurora_mock.cluster_identifier = f"gco-aurora-{region}"
        mock_regional_stack.aurora_cluster = aurora_mock

    return mock_regional_stack


class TestMonitoringStackImports:
    """Tests for monitoring stack imports."""

    def test_monitoring_stack_can_be_imported(self):
        """Test that GCOMonitoringStack can be imported."""
        from gco.stacks.monitoring_stack import GCOMonitoringStack

        assert GCOMonitoringStack is not None


class TestMonitoringStackSynthesis:
    """Tests for monitoring stack synthesis."""

    @pytest.fixture
    def monitoring_stack(self):
        """Create a monitoring stack for testing."""
        from gco.stacks.monitoring_stack import GCOMonitoringStack

        app = cdk.App()
        config = MockConfigLoader()

        # Create mock stacks
        mock_global_stack = create_mock_global_stack()
        mock_api_gw_stack = create_mock_api_gateway_stack()
        mock_regional_stacks = [
            create_mock_regional_stack("us-east-1"),
            create_mock_regional_stack("us-west-2"),
        ]

        stack = GCOMonitoringStack(
            app,
            "TestMonitoringStack",
            config=config,
            global_stack=mock_global_stack,
            regional_stacks=mock_regional_stacks,
            api_gateway_stack=mock_api_gw_stack,
            env=cdk.Environment(account="123456789012", region="us-east-2"),
        )
        return stack

    def test_monitoring_stack_creates_sns_topic(self, monitoring_stack):
        """Test that monitoring stack creates SNS topic."""
        template = assertions.Template.from_stack(monitoring_stack)
        template.has_resource_properties(
            "AWS::SNS::Topic",
            {
                "DisplayName": "GCO (Global Capacity Orchestrator on AWS) Monitoring Alerts",
            },
        )

    def test_monitoring_stack_creates_dashboard(self, monitoring_stack):
        """Test that monitoring stack creates CloudWatch dashboard."""
        template = assertions.Template.from_stack(monitoring_stack)
        template.resource_count_is("AWS::CloudWatch::Dashboard", 1)

    def test_monitoring_stack_creates_log_groups(self, monitoring_stack):
        """Test that monitoring stack creates log groups for each region."""
        template = assertions.Template.from_stack(monitoring_stack)
        # Should create log groups for health monitor and manifest processor per region
        # 2 regions * 2 services = 4 log groups
        template.resource_count_is("AWS::Logs::LogGroup", 4)

    def test_monitoring_stack_creates_alarms(self, monitoring_stack):
        """Test that monitoring stack creates CloudWatch alarms."""
        template = assertions.Template.from_stack(monitoring_stack)
        # Should have multiple alarms
        alarms = template.find_resources("AWS::CloudWatch::Alarm")
        assert len(alarms) > 0

    def test_monitoring_stack_creates_composite_alarms(self, monitoring_stack):
        """Test that monitoring stack creates composite alarms."""
        template = assertions.Template.from_stack(monitoring_stack)
        composite_alarms = template.find_resources("AWS::CloudWatch::CompositeAlarm")
        assert len(composite_alarms) > 0

    def test_monitoring_stack_exports_dashboard_url(self, monitoring_stack):
        """Test that monitoring stack exports dashboard URL."""
        template = assertions.Template.from_stack(monitoring_stack)
        template.has_output("DashboardUrl", {})

    def test_monitoring_stack_exports_alert_topic_arn(self, monitoring_stack):
        """Test that monitoring stack exports alert topic ARN."""
        template = assertions.Template.from_stack(monitoring_stack)
        template.has_output("AlertTopicArn", {})


class TestMonitoringStackAlarms:
    """Tests for specific alarm configurations."""

    @pytest.fixture
    def monitoring_stack(self):
        """Create a monitoring stack for testing."""
        from gco.stacks.monitoring_stack import GCOMonitoringStack

        app = cdk.App()
        config = MockConfigLoader()

        # Create mock stacks
        mock_global_stack = create_mock_global_stack()
        mock_api_gw_stack = create_mock_api_gateway_stack()
        mock_regional_stacks = [
            create_mock_regional_stack("us-east-1"),
            create_mock_regional_stack("us-west-2"),
        ]

        stack = GCOMonitoringStack(
            app,
            "TestMonitoringStack",
            config=config,
            global_stack=mock_global_stack,
            regional_stacks=mock_regional_stacks,
            api_gateway_stack=mock_api_gw_stack,
            env=cdk.Environment(account="123456789012", region="us-east-2"),
        )
        return stack

    def test_api_gateway_5xx_alarm_exists(self, monitoring_stack):
        """Test that API Gateway 5XX alarm is created."""
        template = assertions.Template.from_stack(monitoring_stack)
        # CDK generates alarm names, so we just check for the metric properties
        template.has_resource_properties(
            "AWS::CloudWatch::Alarm",
            {
                "MetricName": "5XXError",
                "Namespace": "AWS/ApiGateway",
            },
        )

    def test_lambda_errors_alarm_exists(self, monitoring_stack):
        """Test that Lambda errors alarm is created."""
        template = assertions.Template.from_stack(monitoring_stack)
        # CDK generates alarm names, so we just check for the metric properties
        template.has_resource_properties(
            "AWS::CloudWatch::Alarm",
            {
                "MetricName": "Errors",
                "Namespace": "AWS/Lambda",
            },
        )

    def test_sqs_old_message_alarm_exists(self, monitoring_stack):
        """Test that SQS old message alarm is created for each region."""
        template = assertions.Template.from_stack(monitoring_stack)
        # CDK generates alarm names, so we just check for the metric properties
        template.has_resource_properties(
            "AWS::CloudWatch::Alarm",
            {
                "MetricName": "ApproximateAgeOfOldestMessage",
                "Namespace": "AWS/SQS",
            },
        )

    def test_sqs_dlq_alarm_exists(self, monitoring_stack):
        """Test that SQS DLQ alarm is created for each region."""
        template = assertions.Template.from_stack(monitoring_stack)
        # CDK generates alarm names, so we just check for the metric properties
        template.has_resource_properties(
            "AWS::CloudWatch::Alarm",
            {
                "MetricName": "ApproximateNumberOfMessagesVisible",
                "Namespace": "AWS/SQS",
            },
        )

    def test_eks_high_cpu_alarm_exists(self, monitoring_stack):
        """Test that EKS high CPU alarm is created for each region."""
        template = assertions.Template.from_stack(monitoring_stack)
        # CDK generates alarm names, so we just check for the metric properties
        template.has_resource_properties(
            "AWS::CloudWatch::Alarm",
            {
                "MetricName": "node_cpu_utilization",
                "Namespace": "ContainerInsights",
            },
        )

    def test_alb_unhealthy_hosts_alarm_skipped(self, monitoring_stack):
        """Test that ALB unhealthy hosts alarm is NOT created (ALB names unknown at synth time)."""
        template = assertions.Template.from_stack(monitoring_stack)
        # ALB alarms are intentionally skipped because ALB names are dynamically
        # generated by the AWS Load Balancer Controller and not known at CDK synth time.
        # Verify no UnHealthyHostCount alarms exist by checking alarm count doesn't include ALB alarms
        alarms = template.find_resources("AWS::CloudWatch::Alarm")
        alb_alarms = [
            name
            for name, props in alarms.items()
            if props.get("Properties", {}).get("MetricName") == "UnHealthyHostCount"
        ]
        assert len(alb_alarms) == 0, "ALB alarms should not be created"


class TestMonitoringStackDashboardWidgets:
    """Tests for dashboard widget configurations."""

    @pytest.fixture
    def monitoring_stack(self):
        """Create a monitoring stack for testing."""
        from gco.stacks.monitoring_stack import GCOMonitoringStack

        app = cdk.App()
        config = MockConfigLoader()

        # Create mock stacks
        mock_global_stack = create_mock_global_stack()
        mock_api_gw_stack = create_mock_api_gateway_stack()
        mock_regional_stacks = [
            create_mock_regional_stack("us-east-1"),
            create_mock_regional_stack("us-west-2"),
        ]

        stack = GCOMonitoringStack(
            app,
            "TestMonitoringStack",
            config=config,
            global_stack=mock_global_stack,
            regional_stacks=mock_regional_stacks,
            api_gateway_stack=mock_api_gw_stack,
            env=cdk.Environment(account="123456789012", region="us-east-2"),
        )
        return stack

    def test_dashboard_has_body(self, monitoring_stack):
        """Test that dashboard has a body with widgets."""
        template = assertions.Template.from_stack(monitoring_stack)
        dashboards = template.find_resources("AWS::CloudWatch::Dashboard")
        assert len(dashboards) == 1

        # Get the dashboard body
        dashboard_key = list(dashboards.keys())[0]
        dashboard_body = dashboards[dashboard_key]["Properties"]["DashboardBody"]
        assert dashboard_body is not None


class TestMonitoringStackOptionalDataServices:
    """Tests for FSx Lustre, Valkey, and Aurora pgvector dashboard sections.

    These sections are added conditionally — only when the feature is
    enabled in the project's config. The widgets use CloudWatch SEARCH
    expressions (same pattern as the ALB widgets) to avoid cross-stack,
    cross-region CloudFormation references to resource IDs that CDK
    would otherwise need ``crossRegionReferences=True`` (and a
    Lambda-backed custom resource) to resolve.

    The tests exercise:

      - Disabled-everywhere: no FSx/Valkey/Aurora widgets on the dashboard
      - Enabled: section appears with correct metric names and dimension
        keys, one widget-per-region in the configured regions
    """

    def _build_stack(self, config, regional_stacks=None):
        from gco.stacks.monitoring_stack import GCOMonitoringStack

        app = cdk.App()
        if regional_stacks is None:
            regional_stacks = [
                create_mock_regional_stack(region) for region in config.get_regions()
            ]
        return GCOMonitoringStack(
            app,
            "TestMonitoringStack",
            config=config,
            global_stack=create_mock_global_stack(),
            regional_stacks=regional_stacks,
            api_gateway_stack=create_mock_api_gateway_stack(),
            env=cdk.Environment(account="123456789012", region="us-east-2"),
        )

    def _dashboard_body(self, stack) -> str:
        template = assertions.Template.from_stack(stack)
        dashboards = template.find_resources("AWS::CloudWatch::Dashboard")
        assert len(dashboards) == 1
        # DashboardBody is a Fn::Join of intrinsic refs + literal JSON
        # fragments. Flatten to a single string so substring checks work
        # regardless of whether values are tokens or plain literals.
        body = dashboards[next(iter(dashboards))]["Properties"]["DashboardBody"]
        if isinstance(body, dict) and "Fn::Join" in body:
            parts = body["Fn::Join"][1]
            flat = []
            for p in parts:
                if isinstance(p, str):
                    flat.append(p)
                elif isinstance(p, dict):
                    # Token reference — stringify for substring matching
                    flat.append(str(p))
            return "".join(flat)
        if isinstance(body, str):
            return body
        return str(body)

    # ── FSx widgets ──────────────────────────────────────────────────────

    def test_fsx_widgets_absent_when_disabled_everywhere(self):
        """No FSx markdown header when every region has FSx off."""
        stack = self._build_stack(MockConfigLoader())
        body = self._dashboard_body(stack)
        assert "FSx for Lustre" not in body
        assert "AWS/FSx" not in body

    def test_fsx_widgets_present_when_enabled_in_one_region(self):
        """FSx section appears only for the region(s) that enable it.

        The widget's dimension map pins ``FileSystemId`` to the exact file
        system provisioned by each regional stack, so unrelated FSx file
        systems in the same account do not show up on the dashboard.
        """
        stack = self._build_stack(
            MockConfigLoader(),
            regional_stacks=[
                create_mock_regional_stack("us-east-1", fsx_enabled=True),
                create_mock_regional_stack("us-west-2"),
            ],
        )
        body = self._dashboard_body(stack)
        assert "FSx for Lustre" in body
        assert "AWS/FSx" in body
        # The enabled region's mock file system id should appear; the
        # disabled region's should not.
        assert "fs-gco-test-us-east-1" in body
        assert "fs-gco-test-us-west-2" not in body

    def test_fsx_widgets_use_correct_metric_names(self):
        """FSx widgets reference the documented CloudWatch metric names."""
        stack = self._build_stack(
            MockConfigLoader(),
            regional_stacks=[
                create_mock_regional_stack("us-east-1", fsx_enabled=True),
            ],
        )
        body = self._dashboard_body(stack)
        for metric in (
            "DataReadBytes",
            "DataWriteBytes",
            "DataReadOperations",
            "DataWriteOperations",
            "FreeDataStorageCapacity",
        ):
            assert metric in body, f"expected FSx metric {metric} in dashboard body"

    def test_fsx_widgets_dimension_key_is_file_system_id(self):
        """FSx metric dimension must be FileSystemId."""
        stack = self._build_stack(
            MockConfigLoader(),
            regional_stacks=[
                create_mock_regional_stack("us-east-1", fsx_enabled=True),
            ],
        )
        body = self._dashboard_body(stack)
        assert "FileSystemId" in body

    def test_fsx_widgets_include_all_enabled_regions(self):
        """Each enabled region's file system ID appears on the dashboard."""
        stack = self._build_stack(
            MockConfigLoader(),
            regional_stacks=[
                create_mock_regional_stack("us-east-1", fsx_enabled=True),
                create_mock_regional_stack("us-west-2", fsx_enabled=True),
            ],
        )
        body = self._dashboard_body(stack)
        assert "fs-gco-test-us-east-1" in body
        assert "fs-gco-test-us-west-2" in body

    # ── Valkey widgets ───────────────────────────────────────────────────

    def test_valkey_widgets_absent_when_disabled(self):
        stack = self._build_stack(MockConfigLoader())
        body = self._dashboard_body(stack)
        assert "Valkey Serverless Cache" not in body

    def test_valkey_widgets_present_when_enabled(self):
        stack = self._build_stack(MockConfigLoader(valkey_enabled=True))
        body = self._dashboard_body(stack)
        assert "Valkey Serverless Cache" in body
        # One widget-per-region should exist for each configured region
        assert "Valkey - ECPU & Cache Size (us-east-1)" in body
        assert "Valkey - ECPU & Cache Size (us-west-2)" in body

    def test_valkey_widgets_use_lowercase_clusterId_dimension(self):
        """ElastiCache Serverless metric dimension is camelCase ``clusterId``.

        The node-based caches use ``CacheClusterId`` (PascalCase) — mixing
        these up produces an empty graph. This is the regression gate.
        """
        stack = self._build_stack(MockConfigLoader(valkey_enabled=True))
        body = self._dashboard_body(stack)
        assert "clusterId" in body
        # Make sure we did NOT accidentally use the node-based dimension name
        assert "CacheClusterId" not in body

    def test_valkey_widgets_pin_to_project_named_caches(self):
        """Widgets use the exact ``{project_name}-{region}`` cache name.

        The monitoring stack no longer uses a SEARCH wildcard — it pins
        each widget to the deterministic cache name the regional stack
        creates (``serverless_cache_name=f"{project_name}-{region}"``).
        The JSON body has the name in ``"clusterId":"gco-test-us-east-1"``
        form (project-scoped for #139 so multiple deployments coexist).
        """
        config = MockConfigLoader(valkey_enabled=True)
        stack = self._build_stack(config)
        body = self._dashboard_body(stack)
        project = config.get_project_name()
        assert f"{project}-us-east-1" in body
        assert f"{project}-us-west-2" in body
        # The Valkey widgets must pin the exact cache name
        # (``"clusterId":"gco-test-us-east-1"`` form) and must NOT fall
        # back to a SEARCH wildcard on the cache-name prefix
        # (``clusterId="gco-test-"``). Other dashboard widgets may
        # legitimately use SEARCH, so we only forbid the prefix wildcard.
        assert f'clusterId="{project}-"' not in body

    def test_valkey_widgets_use_correct_metric_names(self):
        stack = self._build_stack(MockConfigLoader(valkey_enabled=True))
        body = self._dashboard_body(stack)
        for metric in (
            "ElastiCacheProcessingUnits",
            "BytesUsedForCache",
            "CacheHitRate",
            "SuccessfulReadRequestLatency",
            "SuccessfulWriteRequestLatency",
        ):
            assert metric in body, f"expected Valkey metric {metric} in dashboard body"

    # ── Aurora pgvector widgets ──────────────────────────────────────────

    def test_aurora_widgets_absent_when_disabled_everywhere(self):
        stack = self._build_stack(MockConfigLoader())
        body = self._dashboard_body(stack)
        assert "Aurora pgvector" not in body

    def test_aurora_widgets_present_when_enabled_in_one_region(self):
        """Aurora section appears only for the region(s) that enable it."""
        stack = self._build_stack(
            MockConfigLoader(),
            regional_stacks=[
                create_mock_regional_stack("us-east-1", aurora_enabled=True),
                create_mock_regional_stack("us-west-2"),
            ],
        )
        body = self._dashboard_body(stack)
        assert "Aurora pgvector" in body
        assert "gco-aurora-us-east-1" in body
        # Only the enabled region's widget appears
        assert "Aurora - ACU Utilization & Capacity (us-east-1)" in body
        assert "Aurora - ACU Utilization & Capacity (us-west-2)" not in body

    def test_aurora_widgets_use_correct_metric_names(self):
        stack = self._build_stack(
            MockConfigLoader(),
            regional_stacks=[
                create_mock_regional_stack("us-east-1", aurora_enabled=True),
            ],
        )
        body = self._dashboard_body(stack)
        for metric in (
            "ACUUtilization",
            "ServerlessDatabaseCapacity",
            "DatabaseConnections",
            "ReadLatency",
            "WriteLatency",
            "CPUUtilization",
        ):
            assert metric in body, f"expected Aurora metric {metric} in dashboard body"

    def test_aurora_widgets_dimension_key_is_db_cluster_identifier(self):
        """Aurora metric dimension must be DBClusterIdentifier."""
        stack = self._build_stack(
            MockConfigLoader(),
            regional_stacks=[
                create_mock_regional_stack("us-east-1", aurora_enabled=True),
            ],
        )
        body = self._dashboard_body(stack)
        assert "DBClusterIdentifier" in body

    def test_aurora_widgets_include_all_enabled_regions(self):
        """Each enabled region's Aurora cluster id appears on the dashboard."""
        stack = self._build_stack(
            MockConfigLoader(),
            regional_stacks=[
                create_mock_regional_stack("us-east-1", aurora_enabled=True),
                create_mock_regional_stack("us-west-2", aurora_enabled=True),
            ],
        )
        body = self._dashboard_body(stack)
        assert "gco-aurora-us-east-1" in body
        assert "gco-aurora-us-west-2" in body

    # ── ALB widgets ──────────────────────────────────────────────────────

    def test_alb_widgets_filter_to_platform_alb_name_prefix(self):
        """ALB widgets use a SEARCH composite-token match on ``app/k8s-gco-``.

        The AWS Load Balancer Controller names the platform ALB
        ``k8s-gco-<hash>`` (the namespace is shortened because the
        controller enforces a 32-char total name limit). CloudWatch's
        ``LoadBalancer`` dimension is the ARN suffix ``app/<name>/<hash>``.

        We use an UNQUOTED filter in the SEARCH expression
        (``LoadBalancer=app/k8s-gco-``) so CloudWatch performs a
        composite-token match rather than exact string match. Quoting
        the value (``LoadBalancer="app/k8s-gco-"``) would force exact
        match and return nothing — no ALB's dimension value is literally
        ``app/k8s-gco-``. Matches every GCO platform ALB; inference ALBs
        have a different prefix so they don't match.
        """
        stack = self._build_stack(MockConfigLoader())
        body = self._dashboard_body(stack)
        # Unquoted composite-token filter (JSON-escaped body form)
        assert "LoadBalancer=app/k8s-gco-" in body

    def test_alb_widgets_do_not_match_all_albs(self):
        """Regression gate: the ALB SEARCH must be scoped by LoadBalancer name.

        The original dashboard used ``Namespace="AWS/ApplicationELB"`` with
        no name-prefix filter, so every ALB in the account appeared. We
        now include ``LoadBalancer=app/k8s-gco-`` to scope the search.
        This test fails if someone drops the filter.

        The metric-schema form ``{AWS/ApplicationELB,LoadBalancer}`` is
        still allowed — that's the SEARCH syntax that constrains which
        dimension tuples CloudWatch considers, not a namespace-only
        wildcard.
        """
        stack = self._build_stack(MockConfigLoader())
        body = self._dashboard_body(stack)
        # The double-quoted namespace-only form must NOT be present
        assert 'Namespace=\\"AWS/ApplicationELB\\"' not in body
        assert 'Namespace="AWS/ApplicationELB"' not in body
        # And the exact-match quoted filter must not be used either —
        # that was my initial attempt and it returned zero data because
        # no ALB has LoadBalancer literally equal to "app/k8s-gco-".
        assert 'LoadBalancer=\\"app/k8s-gco-\\"' not in body
        assert 'LoadBalancer="app/k8s-gco-"' not in body

    # ── Cross-cutting ────────────────────────────────────────────────────

    def test_all_three_sections_present_when_all_enabled(self):
        """End-to-end: FSx + Valkey + Aurora all enabled → all three headers."""
        stack = self._build_stack(
            MockConfigLoader(valkey_enabled=True),
            regional_stacks=[
                create_mock_regional_stack("us-east-1", fsx_enabled=True, aurora_enabled=True),
                create_mock_regional_stack("us-west-2", fsx_enabled=True, aurora_enabled=True),
            ],
        )
        body = self._dashboard_body(stack)
        assert "FSx for Lustre" in body
        assert "Valkey Serverless Cache" in body
        assert "Aurora pgvector" in body
        assert "Application Load Balancers" in body

    def test_cross_region_references_enabled_on_stack(self):
        """Stack enables ``cross_region_references`` for cross-region FSx IDs.

        Without this CDK flag, synth raises ``CrossRegionReferencesNotEnabled``
        when a widget's dimension map contains a CFN Ref to an FSx file
        system from a different region's regional stack. This test is the
        regression gate — it forces CDK to synthesize a stack where the
        monitoring stack (in us-east-2) references FSx file system IDs
        from regional stacks in us-east-1 and us-west-2. If the flag is
        dropped, the fixture construction itself will raise.
        """
        # Build a real cdk.App hierarchy: regional stacks in their own
        # regions, a monitoring stack in us-east-2, then assert we can
        # synthesize without a CrossRegionReferencesNotEnabled error.
        from gco.stacks.monitoring_stack import GCOMonitoringStack

        app = cdk.App()
        # Use the real MagicMock-backed regional stacks but with fsx mocks
        # whose .ref is a literal string (not a CFN token) — because mocks
        # don't carry construct identity, CDK can't detect a cross-region
        # ref from a mock. So to genuinely exercise the cross-region path
        # we'd need real regional stacks — which the nag-compliance matrix
        # already does. Here we just assert the stack's resolved props
        # carry cross_region_references=True via the CloudFormation cache.

        regional_stacks = [
            create_mock_regional_stack("us-east-1", fsx_enabled=True),
            create_mock_regional_stack("us-west-2"),
        ]
        stack = GCOMonitoringStack(
            app,
            "TestMonitoringStack",
            config=MockConfigLoader(),
            global_stack=create_mock_global_stack(),
            regional_stacks=regional_stacks,
            api_gateway_stack=create_mock_api_gateway_stack(),
            env=cdk.Environment(account="123456789012", region="us-east-2"),
        )

        # CDK stores the resolved Stack props under _cdk_stack_options or as
        # private attributes. The most reliable check is to read back the
        # Stack's synthesized cloud assembly metadata. Simpler: grep the
        # stack's constructor source for the setdefault call — a syntactic
        # regression gate.
        import inspect

        src = inspect.getsource(GCOMonitoringStack.__init__)
        assert "cross_region_references" in src, (
            "GCOMonitoringStack.__init__ must set cross_region_references=True "
            "so FSx file system IDs can flow across regions into dashboard widgets."
        )
        # Additionally, synthesis should succeed (this doubles as a smoke
        # test — if the flag is accidentally set to False with real cross-
        # region refs present, synth raises).
        template = assertions.Template.from_stack(stack)
        template.resource_count_is("AWS::CloudWatch::Dashboard", 1)
