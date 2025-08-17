import json
import boto3
import logging
from botocore.exceptions import ClientError

# Initialize logger
logger = logging.getLogger()
logger.setLevel(logging.INFO) # Set to logging.DEBUG for more verbose logs

# --- GLOBAL STATIC CONFIGURATION VARIABLES ---
HOSTED_ZONE_ID = "Z00000000X0XXXXXXXXX" # Your Route 53 Hosted Zone ID
DNS_RECORD_TTL = 30                     # Time-to-Live for the DNS record (in seconds)
# --- END GLOBAL STATIC CONFIGURATION VARIABLES ---


def lambda_handler(event, context):
    logger.info(f"Received event: {json.dumps(event)}")

    # Extract instance-id and region from the EC2 State-change event
    detail = event.get('detail', {})
    instance_id = detail.get('instance-id')
    region = event.get('region')

    # --- Input Validation for Event ---
    if not instance_id or not region:
        error_body = {
            "status": "error",
            "message": "Missing 'instance-id' or 'region' in the EC2 state-change event detail.",
            "event": event
        }
        logger.error(f"Validation Error: {error_body['message']} Event: {json.dumps(event)}")
        return {
            "statusCode": 400,
            "body": json.dumps(error_body, default=str)
        }

    logger.info(f"Processing EC2 instance '{instance_id}' in region '{region}' for DNS update.")

    try:
        # Initialize Boto3 clients
        ec2 = boto3.client('ec2', region_name=region)

        update_results = []

        # Describe the instance to get the public IP and tags
        try:
            described_instances_response = ec2.describe_instances(InstanceIds=[instance_id])
            reservations = described_instances_response.get('Reservations', [])
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            error_message = e.response.get("Error", {}).get("Message")
            logger.error(f"AWS API Error describing instance {instance_id}: [{error_code}] {error_message}", exc_info=True)
            return {
                "statusCode": 404,
                "body": json.dumps({"status": "error", "message": f"Failed to describe instance {instance_id}: {error_message}"}, default=str)
            }
        except Exception as e:
            logger.critical(f"Unexpected error describing instance {instance_id}: {e}", exc_info=True)
            return {
                "statusCode": 500,
                "body": json.dumps({"status": "error", "message": f"Unexpected error describing instance {instance_id}: {str(e)}"}, default=str)
            }

        found_instance_details = False
        for reservation in reservations:
            for instance in reservation.get('Instances', []):
                found_instance_details = True

                # Get the PublicDNS tag value
                dns_name = None
                ec2_name = None

                if 'Tags' in instance:
                    for tag in instance['Tags']:
                        if tag['Key'] == 'PublicDNS':
                            dns_name = tag['Value']
                        if tag['Key'] == 'Name':
                            ec2_name = tag['Value']

                if not dns_name: # This condition handles both 'tag not present' and 'tag is empty string'
                    if ec2_name: # Check if the Name tag was found and has a value
                            dns_name = ec2_name
                            logger.info(f"'PublicDNS' tag was missing or empty for instance '{instance['InstanceId']}'. Falling back to 'Name' tag: '{ec2_name}'.")
                    else: # Log specific warnings if neither tag provided a usable name
                        logger.warning(f"Neither 'PublicDNS' nor 'Name' tags found for instance '{instance['InstanceId']}'.")
                        return {
                            "statusCode": 400,
                            "body": json.dumps({
                                "status": "error",
                                "message": f"Cannot determine DNS name for instance '{instance['InstanceId']}': No tags found on the instance."
                            })
                        }

                # Get the public IP address
                ip_address = instance.get('PublicIpAddress', None)

                if dns_name and ip_address:
                    logger.info(f"Found instance '{instance['InstanceId']}', DNS name: '{dns_name}', Public IP: '{ip_address}'. Attempting DNS update.")
                    # Perform the DNS update
                    update_result = do_dns_update(HOSTED_ZONE_ID, dns_name, ip_address, DNS_RECORD_TTL)
                    update_results.append(update_result)
                else:
                    # Domain name (from tag or fallback) or public IP is missing
                    reason_msg = ""
                    if not dns_name:
                        reason_msg += f"Missing or empty 'PublicDNS' and no usable 'Name' tag."
                    if not ip_address:
                        if reason_msg: reason_msg += " And "
                        reason_msg += "missing 'PublicIpAddress'."

                    msg = f"Skipping instance '{instance['InstanceId']}': {reason_msg}"
                    logger.warning(msg)
                    update_results.append({
                        "instance_id": instance['InstanceId'],
                        "action": "skipped",
                        "reason": msg,
                        "status": "skipped"
                    })

        if not found_instance_details:
            error_body = {
                "status": "error",
                "message": f"Instance with ID '{instance_id}' not found or could not be described.",
                "instance_id": instance_id,
                "region": region
            }
            logger.error(f"Execution Error: {error_body['message']}")
            return {
                "statusCode": 404,
                "body": json.dumps(error_body, default=str)
            }

        # Determine overall status for the response
        overall_status_message = "DNS update process completed successfully."
        final_status_code = 200

        if any(result.get('status') == 'error' for result in update_results):
            overall_status_message = "Some DNS updates failed. Check 'updates' for details."

        if not update_results:
             overall_status_message = f"No DNS updates were attempted for instance {instance_id}."
             final_status_code = 200

        response_body = {
            "status": "success" if not any(result.get('status') == 'error' for result in update_results) else "partial_success_with_errors",
            "message": overall_status_message,
            "instance_id": instance_id,
            "region": region,
            "updates": update_results
        }

        logger.info(f"Function execution completed with status: {response_body['status']}")
        return {
            "statusCode": final_status_code,
            "body":  json.dumps(response_body, default=str)
        }

    except Exception as e:
        error_body = {
            "status": "error",
            "message": f"An unexpected error occurred during execution: {str(e)}",
            "instance_id": instance_id,
            "region": region,
            "event": event
        }
        logger.critical(f"Unhandled critical exception in lambda_handler: {str(e)}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps(error_body, default=str)
        }


### Helper Functions ###

def do_dns_update(hosted_zone_id, dns_name, ip_address, ttl):
    # Upserts (creates or updates) a DNS record in Route 53 for the given domain name.

    logger.info(f"Attempting UPSERT for DNS record: {dns_name} (Type: A, TTL: {ttl}) to IP: {ip_address} in Hosted Zone: {hosted_zone_id}")
    try:
        route53 = boto3.client('route53')
        response = route53.change_resource_record_sets(
            HostedZoneId=hosted_zone_id,
            ChangeBatch={
                'Comment': 'Updated by Lambda upon EC2 state change',
                'Changes': [
                    {
                        'Action': 'UPSERT',
                        'ResourceRecordSet': {
                            'Name': dns_name,
                            'Type': 'A',
                            'TTL': ttl,
                            'ResourceRecords': [{'Value': ip_address}]
                        }
                    }
                ]
            }
        )
        change_info = response.get("ChangeInfo", {})
        logger.info(f"Successfully UPSERTed DNS record for {dns_name} -> {ip_address}. Change ID: {change_info.get('Id')}")
        logger.debug(f"Route53 Change Info: {json.dumps(change_info, default=str)}")

        return {
            "dns_name": dns_name,
            "ip_address": ip_address,
            "action": "UPSERT",
            "change_info": change_info,
            "status": "success"
        }

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code")
        error_message = e.response.get("Error", {}).get("Message")
        error_msg = f"AWS API Error updating DNS record for {dns_name} to {ip_address}: [{error_code}] {error_message}"
        logger.error(error_msg, exc_info=True)
        return {
            "dns_name": dns_name,
            "ip_address": ip_address,
            "action": "UPSERT",
            "status": "error",
            "error_code": error_code,
            "error_message": error_message
        }
    except Exception as e:
        error_msg = f"Unexpected error updating DNS record for {dns_name} to {ip_address}: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return {
            "dns_name": dns_name,
            "ip_address": ip_address,
            "action": "UPSERT",
            "status": "error",
            "error_message": error_msg
        }
