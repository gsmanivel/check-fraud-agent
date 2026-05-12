import os, json, logging
from azure.servicebus import ServiceBusClient, ServiceBusMessage

logger = logging.getLogger(__name__)


def enqueue_message(queue_name: str, payload: dict):
    conn_str = os.environ["SERVICE_BUS_CONNECTION_STRING"]
    with ServiceBusClient.from_connection_string(conn_str) as client:
        with client.get_queue_sender(queue_name) as sender:
            sender.send_messages(ServiceBusMessage(json.dumps(payload)))
    logger.info(f"Enqueued message to {queue_name} for check {payload.get('id')}")
