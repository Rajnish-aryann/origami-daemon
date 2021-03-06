# For python2 to handle imports properly
from __future__ import absolute_import, unicode_literals

import json
import logging
import os
import re
import uuid

from docker import APIClient
from docker.errors import NotFound, APIError, BuildError

from .celery import app
from .constants import ORIGAMI_CONFIG_DIR, ORIGAMI_DEMOS_DIRNAME, \
    ORIGAMI_WRAPPED_DEMO_PORT, DOCKER_UNIX_SOCKET, ORIGAMI_DEPLOY_LOGS_DIR, \
    LOGS_FILE_MODE_REQ
from .database import Demos, get_a_free_port
from .docker import docker_client
from .exceptions import OrigamiDockerConnectionError
from .logger import OrigamiLogger
from .utils.file import get_origami_static_dir

logger = OrigamiLogger(console_log_level=logging.DEBUG)
logger.disable_file_logging()


def update_demo_status(demo):
    """
    Update current demo status by looking up for the demo container
    using docker.

    Args:
        demo(Demos): Demo table object.
    """
    logging.info('Updating the status of demo : {}'.format(demo.id))
    try:
        if demo.container_id:
            container = docker_client.containers.get(demo.container_id)
            logging.info('Updated demo status from {} to {}'.format(
                demo.status, container.status))
            demo.status = container.status
            demo.save()

    except NotFound:
        logging.info(
            'No container instance found for demo : {} and id : {}'.format(
                demo.demo_id, demo.container_id))
        demo.container_id = None
        demo.status = 'empty'
        demo.save()

    except APIError as e:
        raise OrigamiDockerConnectionError(
            'Error while communicating to to docker API: {}'.format(e))


@app.task()
def remove_demo_instance_if_exist(demo_id, status='empty'):
    """
    Checks if an instance is running for the demo provided with ID
    demo_id, if it exist then remove the container and update the database
    with the status provided. The default status is 'empty'.

    Returns an instance of `Demos` from database or None.

    Args:
        demo_id: ID for the demo from origami server
        status: Status to set the demo in after the container is deleted

    Returns:
        demo (None, Demos): A Demos object or None

    Raises:
        OrigamiDockerConnectionError: Exception when there is an error
            communicating to Docker API.
    """
    logging.info('Checking if the demo instance exist')
    demo = Demos.get_or_none(Demos.demo_id == demo_id)
    if demo and demo.container_id:
        # If there exist a demo which is not empty then delete the instance
        try:
            container = docker_client.containers.get(demo.container_id)
            logging.info('Demo {} is in {} state'.format(
                demo_id, container.status))

            logging.info('Container instance with id {} found'.format(
                demo.container_id))

            # Try stopping the container first
            logging.info('Removing container instance for demo')
            container.stop(timeout=10)

            # Check if the container exist after stopping, if it exist
            # Remove it
            try:
                container = docker_client.containers.get(demo.container_id)
                if container:
                    container.remove()
            except NotFound:
                pass

            logging.info('Container instance removed')
            demo.status = status
            demo.container_id = None
            demo.image_id = None
            demo.save()

            return demo
        except NotFound:
            logging.info(
                'No container instance found for demo : {} and id : {}'.format(
                    demo_id, demo.container_id))
        except APIError as e:
            demo.status = 'error'
            demo.save()
            raise OrigamiDockerConnectionError(
                'Error while communicating to to docker API: {}'.format(e))
    return None


@app.task()
def deploy_demo(demo_id, demo_dir):
    """
    Checks for the existence of demo container and redploy it if it exist

    Args:
        demo_id: Demo ID for the demo to be deployed(this is a unique ID from
            origami database)
        demo_dir: Absolute path to the demo directory where it was unzipped.
    """
    logging.info('Starting task to deploy demo with id : {}'.format(demo_id))
    # Before doing anything get the previously created image if any.
    try:
        remove_demo_instance_if_exist(demo_id, 'redeploying')
    except OrigamiDockerConnectionError as e:
        logging.error(e)
        return

    demo = Demos.get_or_none(Demos.demo_id == demo_id)
    if not demo:
        demo_logs_uid = uuid.uuid4().hex
        demo = Demos(demo_id=demo_id, log_id=demo_logs_uid, status='deploying')

    # Get the dockerfile from the demo dir from ORIGAMI_CONFIG_HOME
    dockerfile_dir = os.path.join(os.environ['HOME'], ORIGAMI_CONFIG_DIR,
                                  ORIGAMI_DEMOS_DIRNAME, demo_id)
    try:
        # Here we are using low level API bindings provided by docker-py to
        # interact with docker daemon. This enables us to collect image build
        # Logs and provide them to user for debugging purposes.
        logging.info('Trying to build image for demo.')
        cli = APIClient(base_url=DOCKER_UNIX_SOCKET)
        response = [
            json.loads(line.decode().strip())
            for line in cli.build(path=dockerfile_dir)
        ]

        # Write build logs to log file.
        logfile = os.path.join(get_origami_static_dir(),
                               ORIGAMI_DEPLOY_LOGS_DIR, demo.log_id)
        with open(logfile, LOGS_FILE_MODE_REQ) as fp:
            json.dump(response, fp)

        final_res = response[-1]['stream'].strip()
        match_obj = re.match(r'Successfully built (.*)', final_res, re.M | re.I)
        image_id = None

        try:
            match_obj.group(1)
            # SHA256 of the built image.
            image_id = response[-2]['aux']['ID'][7:]
        except IndexError as e:
            raise BuildError(e)
        except Exception as e:
            logging.error(
                "Error while parsing SHA256 ID of the image : {}".format(e))
            raise BuildError(e)

        # This was without using low level dockerpy client, it did not provide
        # logs for the build process.
        # image = docker_client.images.build(path=dockerfile_dir)[0]

        logging.info('Image built : ID: {}'.format(image_id))
        demo.image_id = image_id

        # Run a new container instance for the demo.
        if not demo.port:
            port = get_a_free_port()
            logging.info('New port for demo is {}'.format(port))
            demo.port = port

        port_map = '{}/tcp'.format(ORIGAMI_WRAPPED_DEMO_PORT)
        cont = docker_client.containers.run(
            image_id,
            detach=True,
            name=demo_id,
            ports={port_map: demo.port},
            remove=True)

        logging.info('Demo deployed with container id : {}'.format(cont.id))
        demo.container_id = cont.id
        demo.status = 'running'

    except BuildError as e:
        logging.error('Error while building image for {} : {}'.format(
            demo_id, e))
        demo.status = 'error'
    except APIError as e:
        logging.error(
            'Error while communicating to to docker API: {}'.format(e))
        demo.status = 'error'

    demo.save()
