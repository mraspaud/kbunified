"""Backend stuff."""

import logging

from kbunified.backends.interface import ChatBackend
from kbunified.backends.mattermost import MattermostBackend
from kbunified.backends.rocketchat import RocketChatBackend
from kbunified.backends.slack import SlackBackend

logger = logging.getLogger("backend")

def chat_backends_from_config(**services: dict[str, object]) -> list[ChatBackend]:
    """Create a backend from the config."""
    backends = dict()
    for service_id, config in services.items():
        btype = config.pop("backend")
        logger.debug(f"Creating backend {service_id}")
        if btype == "dummy":
            from kbunified.backends.dummy import DummyBackend
            backends[service_id] = DummyBackend(service_id, **config)
        elif btype == "rocket.chat":
            backends[service_id] = RocketChatBackend(service_id, **config)
        elif btype == "slack":
            backends[service_id] = SlackBackend(service_id, **config)
        elif btype == "mattermost":
            backends[service_id] = MattermostBackend(service_id, **config)
        else:
            raise NotImplementedError(f"Do not know how to communicate to {btype}")
    return backends
