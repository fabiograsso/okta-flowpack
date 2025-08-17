import json
import logging
import boto3
import re # Import the regular expression module
from botocore.exceptions import ClientError

# Initialize logger for better logging to CloudWatch
logger = logging.getLogger()
logger.setLevel(logging.INFO) # Set to logging.DEBUG for more detailed logs


def lambda_handler(event, context):
    # Log the entire incoming event for debugging
    logger.info(f"Received event: {json.dumps(event)}")

    # --- Input Validation for Region ---
    if 'region' not in event or not isinstance(event['region'], str) or not event['region'].strip():
        logger.error("Validation Error: Missing or invalid 'region' in event.")
        return {
            "statusCode": 400,
            "body": json.dumps({"message": "Missing or invalid 'region' in event."})
        }
    region = event['region'].strip()

    # --- Input Validation for Operation ---
    if 'operation' not in event or event['operation'] not in ['start', 'stop']:
        logger.error("Validation Error: Missing or invalid 'operation' in event. Must be 'start' or 'stop'.")
        return {
            "statusCode": 400,
            "body": json.dumps({"message": "Missing or invalid 'operation' in event. Must be 'start' or 'stop'."})
        }
    operation = event['operation']

    # --- Input Validation and Processing for Instances ---
    if 'instances' not in event or not isinstance(event['instances'], str) or not event['instances'].strip():
        logger.error("Validation Error: Missing or invalid 'instances' in event. Must be 'all' or a comma-separated list of EC2 instance IDs.")
        return {
            "statusCode": 400,
            "body": json.dumps({"message": "Missing or invalid 'instances' in event. Must be 'all' or a comma-separated list of EC2 instance IDs."})
        }

    instances_input = event['instances'].strip()
    instances_to_process = []
    failed_initial_validation_instances = [] # To capture instances that failed format validation or 'all' logic

    # Initialize Boto3 EC2 client outside the instance loop but after region validation
    try:
        ec2 = boto3.client('ec2', region_name=region)
    except Exception as e:
        logger.error(f"Failed to initialize EC2 client for region {region}: {e}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({"message": f"Failed to initialize EC2 client for region {region}."})
        }

    # Handle "all" instances logic
    if instances_input.lower() == 'all':
        logger.info(f"Operation '{operation}' requested for ALL EC2 instances in region '{region}'.")
        try:
            # Describe all instances to filter based on current state
            describe_paginator = ec2.get_paginator('describe_instances')
            all_reservations = describe_paginator.paginate()

            for page in all_reservations:
                for reservation in page.get('Reservations', []):
                    for instance in reservation.get('Instances', []):
                        instance_id = instance['InstanceId']
                        instance_state = instance['State']['Name']

                        if operation == 'start' and instance_state in ['stopped', 'stopping']:
                            instances_to_process.append(instance_id)
                        elif operation == 'stop' and instance_state in ['running', 'pending']:
                            instances_to_process.append(instance_id)
                        else:
                            # Log instances that are not in a suitable state for the operation
                            logger.info(f"Skipping instance {instance_id} for '{operation}' operation due to current state: '{instance_state}'.")

            if not instances_to_process:
                logger.warning(f"No instances found in a suitable state ('stopped' for start, 'running' for stop) for '{operation}' operation in region '{region}'.")
                # Return 200 OK, as the operation completed successfully by finding no targets.
                return {
                    "statusCode": 200,
                    "body": json.dumps({
                        "message": f"No instances found in suitable state for '{operation}' operation in region '{region}'.",
                        "operation": operation,
                        "successful_instances": [],
                        "failed_instances": []
                    })
                }

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            error_message = e.response.get("Error", {}).get("Message")
            logger.error(f"AWS API Error describing all instances in {region}: [{error_code}] {error_message}", exc_info=True)
            return {
                "statusCode": 500,
                "body": json.dumps({"message": f"Failed to list all instances in {region}: {error_message}"})
            }
        except Exception as e:
            logger.critical(f"Unexpected error describing all instances in {region}: {e}", exc_info=True)
            return {
                "statusCode": 500,
                "body": json.dumps({"message": f"Unexpected error listing all instances in {region}: {str(e)}"})
            }

    else:
        # Process comma-separated list with instance ID format validation
        instance_ids_raw = instances_input.split(',')
        # Regex for standard EC2 instance ID format: 'i-' followed by 17 lowercase hexadecimal characters
        instance_id_regex = re.compile(r"^i-[0-9a-f]{17}$") 

        for instance_id_str in instance_ids_raw:
            cleaned_id = instance_id_str.strip()
            if not cleaned_id:
                continue # Skip empty strings

            if instance_id_regex.match(cleaned_id):
                instances_to_process.append(cleaned_id)
            else:
                # Add to failed list right away if format is invalid
                failed_initial_validation_instances.append({
                    "instance_id": cleaned_id,
                    "reason": "Invalid EC2 instance ID format (must be i-xxxxxxxxxxxxxxxxx)"
                })
                logger.warning(f"Skipping instance ID '{cleaned_id}' due to invalid format. Must match 'i- followed by 17 hex characters'.")

    # If no valid instances were provided (either empty list or all invalid format)
    if not instances_to_process and not failed_initial_validation_instances:
        logger.error("Validation Error: No valid EC2 instance IDs provided for processing.")
        return {
            "statusCode": 400,
            "body": json.dumps({"message": "No valid EC2 instance IDs provided for processing."})
        }
    elif not instances_to_process and failed_initial_validation_instances:
        # If instances_to_process is empty but failed_initial_validation_instances is not,
        # it means all provided IDs were invalid format.
        logger.error(f"Validation Error: All provided EC2 instance IDs had an invalid format: {failed_initial_validation_instances}")
        return {
            "statusCode": 400,
            "body": json.dumps({
                "message": "All provided EC2 instance IDs had an invalid format.",
                "invalid_instances": failed_initial_validation_instances # Include details of invalid instances
            })
        }

    # --- End Input Validation and Processing for Instances ---

    logger.info(f"Attempting to {operation} the following instances: {instances_to_process} in region: {region}")

    # Initialize lists to store results of the EC2 operations
    successful_operations = []
    failed_operations = []

    # Pre-populate failed_operations with instances that failed initial format validation
    # This ensures they are part of the final error report.
    failed_operations.extend(failed_initial_validation_instances)

    for instance_id in instances_to_process:
        try:
            if operation == 'start':
                ec2.start_instances(InstanceIds=[instance_id])
                logger.info(f"Successfully initiated 'start' for instance: {instance_id}")
            elif operation == 'stop':
                ec2.stop_instances(InstanceIds=[instance_id])
                logger.info(f"Successfully initiated 'stop' for instance: {instance_id}")

            # Append to successful_operations only after the API call succeeds
            successful_operations.append(instance_id)

        except ClientError as e:
            # Granular Exception Handling for AWS API errors during start/stop
            error_code = e.response.get("Error", {}).get("Code")
            error_message = e.response.get("Error", {}).get("Message")
            logger.error(f"AWS API Error during '{operation}' of instance {instance_id}: [{error_code}] {error_message}", exc_info=True)
            failed_operations.append({
                "instance_id": instance_id,
                "reason": f"AWS API Error: [{error_code}] {error_message}"
            })
        except Exception as e:
            # Catch any other unexpected Python exceptions
            logger.error(f"Unexpected error during '{operation}' of instance {instance_id}: {e}", exc_info=True)
            failed_operations.append({
                "instance_id": instance_id,
                "reason": f"Unexpected error: {str(e)}"
            })

    # Determine overall status and structure response
    overall_message = ""
    status_code = 200 # Default to success unless all failed

    if not successful_operations and failed_operations:
        # All attempted operations (including initial format failures) failed
        status_code = 500 # Indicates a problem with the overall execution or input
        overall_message = f"Failed to {operation} any of the specified instances. Check 'failed_instances' for details."
    elif successful_operations and failed_operations:
        # Partial success (some worked, some failed)
        status_code = 200 # Still considered a success for the Lambda invocation, but with warnings
        overall_message = f"Successfully initiated {operation} for some instances, others failed. Check results for details."
    else:
        # All successful (including cases where 'all' was specified but no suitable instances were found)
        overall_message = f"Successfully initiated {operation} for all specified instances."

    # Final response body
    response_body = {
        "message": overall_message,
        "operation": operation,
        "successful_instances": successful_operations,
        "failed_instances": failed_operations,
        "region": region
    }

    logger.info(f"Function execution completed. Status: {status_code}, Message: {overall_message}")
    return {
        "statusCode": status_code,
        "body": json.dumps(response_body) # Body must be a JSON string
    }
