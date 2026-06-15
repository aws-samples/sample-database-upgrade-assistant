"""
Resource Scanner MCP Server — Scans AWS resources for database connections.

Provides scan functions for EC2, Lambda, ECS, EKS, Secrets Manager,
SSM Parameter Store, and CloudFormation. Each function takes a boto3
session (from assumed role), region, and cluster_endpoint, and returns
a list of connected resources.

These are Python functions called directly by the agent (not MCP stdio).
"""

import json
import logging

logger = logging.getLogger(__name__)


def _safe_get(func, description):
    """Wrap an AWS API call with error handling."""
    try:
        return func()
    except Exception as e:
        logger.warning("Failed to %s: %s", description, e)
        return []


def scan_ec2_instances(session, region, cluster_endpoint):
    """
    Scan EC2 instances for references to the cluster endpoint.

    Checks instance tags and user-data for connection strings.

    Returns:
        list of {application_name, deployment_type, deployment_location,
                 region, db_cluster_identifier, connection_source}
    """
    ec2 = session.client("ec2", region_name=region)
    results = []

    try:
        paginator = ec2.get_paginator("describe_instances")
        for page in paginator.paginate():
            for reservation in page.get("Reservations", []):
                for instance in reservation.get("Instances", []):
                    instance_id = instance.get("InstanceId", "")
                    tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
                    name = tags.get("Name", instance_id)

                    # Check tags for cluster references
                    tag_match = any(
                        cluster_endpoint.lower() in str(v).lower()
                        for v in tags.values()
                    )

                    # Check user-data
                    user_data_match = False
                    try:
                        ud_resp = ec2.describe_instance_attribute(
                            InstanceId=instance_id, Attribute="userData"
                        )
                        user_data = ud_resp.get("UserData", {}).get("Value", "")
                        if user_data:
                            import base64
                            decoded = base64.b64decode(user_data).decode("utf-8", errors="replace")
                            user_data_match = cluster_endpoint.lower() in decoded.lower()
                    except Exception:
                        pass

                    if tag_match or user_data_match:
                        connection_source = "Tag reference" if tag_match else "User-data connection string"
                        results.append({
                            "application_name": name,
                            "deployment_type": "EC2",
                            "deployment_location": instance_id,
                            "region": region,
                            "db_cluster_identifier": cluster_endpoint,
                            "connection_source": connection_source,
                        })

        logger.info("scan_ec2_instances: found %d instances", len(results))
    except Exception as e:
        logger.warning("scan_ec2_instances failed: %s", e)

    return results


def scan_lambda_functions(session, region, cluster_endpoint):
    """
    Scan Lambda functions for environment variables referencing the cluster.

    Returns:
        list of resource dicts
    """
    lam = session.client("lambda", region_name=region)
    results = []

    try:
        paginator = lam.get_paginator("list_functions")
        for page in paginator.paginate():
            for func in page.get("Functions", []):
                func_name = func.get("FunctionName", "")
                env_vars = func.get("Environment", {}).get("Variables", {})

                env_match = any(
                    cluster_endpoint.lower() in str(v).lower()
                    for v in env_vars.values()
                )

                if env_match:
                    results.append({
                        "application_name": func_name,
                        "deployment_type": "Lambda",
                        "deployment_location": func.get("FunctionArn", func_name),
                        "region": region,
                        "db_cluster_identifier": cluster_endpoint,
                        "connection_source": "Environment variable",
                    })

        logger.info("scan_lambda_functions: found %d functions", len(results))
    except Exception as e:
        logger.warning("scan_lambda_functions failed: %s", e)

    return results


def scan_ecs_task_definitions(session, region, cluster_endpoint):
    """
    Scan ECS task definitions for environment variables and secrets
    referencing the cluster.

    Returns:
        list of resource dicts
    """
    ecs = session.client("ecs", region_name=region)
    results = []

    try:
        paginator = ecs.get_paginator("list_task_definitions")
        for page in paginator.paginate(status="ACTIVE"):
            for td_arn in page.get("taskDefinitionArns", []):
                try:
                    td_resp = ecs.describe_task_definition(taskDefinition=td_arn)
                    td = td_resp.get("taskDefinition", {})
                    family = td.get("family", "")

                    for container in td.get("containerDefinitions", []):
                        env_vars = container.get("environment", [])
                        secrets = container.get("secrets", [])

                        env_match = any(
                            cluster_endpoint.lower() in str(e.get("value", "")).lower()
                            for e in env_vars
                        )
                        secret_match = any(
                            cluster_endpoint.lower() in str(s.get("valueFrom", "")).lower()
                            for s in secrets
                        )

                        if env_match or secret_match:
                            source = "Environment variable" if env_match else "Secret reference"
                            results.append({
                                "application_name": family,
                                "deployment_type": "ECS",
                                "deployment_location": td_arn,
                                "region": region,
                                "db_cluster_identifier": cluster_endpoint,
                                "connection_source": source,
                            })
                            break  # One match per task def is enough
                except Exception as e:
                    logger.warning("Failed to describe task definition %s: %s", td_arn, e)

        logger.info("scan_ecs_task_definitions: found %d task defs", len(results))
    except Exception as e:
        logger.warning("scan_ecs_task_definitions failed: %s", e)

    return results


def scan_eks_clusters(session, region, appci_tag):
    """
    Scan EKS clusters for matching AppCI tags.

    Returns:
        list of resource dicts
    """
    eks = session.client("eks", region_name=region)
    results = []

    try:
        resp = eks.list_clusters()
        for cluster_name in resp.get("clusters", []):
            try:
                desc = eks.describe_cluster(name=cluster_name)
                cluster = desc.get("cluster", {})
                tags = cluster.get("tags", {})

                appci_match = any(
                    appci_tag.lower() in str(v).lower()
                    for v in tags.values()
                ) or any(
                    appci_tag.lower() in str(k).lower()
                    for k in tags.keys()
                )

                if appci_match:
                    results.append({
                        "application_name": cluster_name,
                        "deployment_type": "EKS",
                        "deployment_location": cluster.get("arn", cluster_name),
                        "region": region,
                        "db_cluster_identifier": "",
                        "connection_source": "Tag reference",
                    })
            except Exception as e:
                logger.warning("Failed to describe EKS cluster %s: %s", cluster_name, e)

        logger.info("scan_eks_clusters: found %d clusters", len(results))
    except Exception as e:
        logger.warning("scan_eks_clusters failed: %s", e)

    return results


def scan_secrets_manager(session, region, cluster_endpoint):
    """
    Scan Secrets Manager for secrets containing the cluster endpoint.

    Returns:
        list of resource dicts
    """
    sm = session.client("secretsmanager", region_name=region)
    results = []

    try:
        paginator = sm.get_paginator("list_secrets")
        for page in paginator.paginate():
            for secret in page.get("SecretList", []):
                secret_name = secret.get("Name", "")
                try:
                    val_resp = sm.get_secret_value(SecretId=secret_name)
                    secret_string = val_resp.get("SecretString", "")
                    if cluster_endpoint.lower() in secret_string.lower():
                        results.append({
                            "application_name": secret_name,
                            "deployment_type": "SecretsManager",
                            "deployment_location": secret.get("ARN", secret_name),
                            "region": region,
                            "db_cluster_identifier": cluster_endpoint,
                            "connection_source": "Secret value",
                        })
                except Exception as e:
                    logger.warning("Failed to read secret %s: %s", secret_name, e)

        logger.info("scan_secrets_manager: found %d secrets", len(results))
    except Exception as e:
        logger.warning("scan_secrets_manager failed: %s", e)

    return results


def scan_ssm_parameters(session, region, cluster_endpoint):
    """
    Scan SSM Parameter Store for parameters containing the cluster endpoint.

    Returns:
        list of resource dicts
    """
    ssm = session.client("ssm", region_name=region)
    results = []

    try:
        paginator = ssm.get_paginator("describe_parameters")
        for page in paginator.paginate():
            for param in page.get("Parameters", []):
                param_name = param.get("Name", "")
                try:
                    val_resp = ssm.get_parameter(Name=param_name, WithDecryption=True)
                    value = val_resp.get("Parameter", {}).get("Value", "")
                    if cluster_endpoint.lower() in value.lower():
                        results.append({
                            "application_name": param_name,
                            "deployment_type": "SSM",
                            "deployment_location": param.get("ARN", param_name),
                            "region": region,
                            "db_cluster_identifier": cluster_endpoint,
                            "connection_source": "Parameter value",
                        })
                except Exception as e:
                    logger.warning("Failed to read SSM param %s: %s", param_name, e)

        logger.info("scan_ssm_parameters: found %d parameters", len(results))
    except Exception as e:
        logger.warning("scan_ssm_parameters failed: %s", e)

    return results


def scan_cloudformation_stacks(session, region, cluster_endpoint):
    """
    Scan CloudFormation stacks for template references to the cluster endpoint.

    Returns:
        list of resource dicts
    """
    cfn = session.client("cloudformation", region_name=region)
    results = []

    try:
        paginator = cfn.get_paginator("list_stacks")
        for page in paginator.paginate(StackStatusFilter=[
            "CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"
        ]):
            for stack_summary in page.get("StackSummaries", []):
                stack_name = stack_summary.get("StackName", "")
                try:
                    tmpl_resp = cfn.get_template(StackName=stack_name)
                    template_body = tmpl_resp.get("TemplateBody", "")
                    if isinstance(template_body, dict):
                        template_body = json.dumps(template_body)
                    if cluster_endpoint.lower() in template_body.lower():
                        results.append({
                            "application_name": stack_name,
                            "deployment_type": "CloudFormation",
                            "deployment_location": stack_summary.get("StackId", stack_name),
                            "region": region,
                            "db_cluster_identifier": cluster_endpoint,
                            "connection_source": "Template reference",
                        })
                except Exception as e:
                    logger.warning("Failed to get template for stack %s: %s", stack_name, e)

        logger.info("scan_cloudformation_stacks: found %d stacks", len(results))
    except Exception as e:
        logger.warning("scan_cloudformation_stacks failed: %s", e)

    return results
