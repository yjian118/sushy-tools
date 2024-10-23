# Copyright 2019 Red Hat, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
# Copyright (c) 2024 Wind River Systems, Inc.
#

import os
import re
import tempfile
import time
from urllib import parse as urlparse

import requests

from sushy_tools.emulator import memoize
from sushy_tools.emulator.resources import base
from sushy_tools import error


class StaticDriver(base.DriverBase):
    """Redfish virtual media simulator."""

    def __init__(self, config, logger):
        super().__init__(config, logger)
        self._devices = memoize.PersistentDict()
        if hasattr(self._devices, 'make_permanent'):
            self._devices.make_permanent(
                self._config.get('SUSHY_EMULATOR_STATE_DIR'), 'vmedia')

        device_types = self._config.get(
            'SUSHY_EMULATOR_VMEDIA_DEVICES')
        if device_types is None:
            device_types = {
                u'Cd': {
                    u'Name': u'Virtual CD',
                    u'MediaTypes': [
                        u'CD',
                        u'DVD'
                    ]
                },
                u'Floppy': {
                    u'Name': u'Virtual Removable Media',
                    u'MediaTypes': [
                        u'Floppy',
                        u'USBStick'
                    ]
                }
            }

        self._device_types = device_types

    def _get_device(self, identity, device):
        try:
            return self._devices[(identity, device)]

        except KeyError:
            self._devices.update(
                {(identity, k): v for k, v in self._device_types.items()})

        try:
            return self._devices[(identity, device)]

        except KeyError:
            raise error.FishyError(
                'No such virtual media device %s owned by resource '
                '%s' % (device, identity))

    @property
    def driver(self):
        """Return human-friendly driver information

        :returns: driver information as `str`
        """
        return '<static-vmedia>'

    @property
    def devices(self):
        """Return available Redfish virtual media devices

        :returns: list of virtual media devices IDs
        """
        return list(self._device_types)

    def get_device_name(self, identity, device):
        """Get virtual media device name

        :param identity: parent resource ID
        :param device: device name
        :returns: virtual media device name
        :raises: `error.FishyError`
        """
        device_info = self._get_device(identity, device)
        return device_info.get('Name', identity)

    def get_device_media_types(self, identity, device):
        """Get supported media types for the device

        :param identity: parent resource ID
        :param device: device name
        :returns: media types supported by this device
        :raises: `error.FishyError`
        """
        device_info = self._get_device(identity, device)
        return device_info.get('MediaTypes', [])

    def get_device_image_info(self, identity, device):
        """Get media state of the virtual media device

        :param identity: parent resource ID
        :param device: device name
        :returns: a `tuple` of: image name, image path, `True` is media is
            inserted, `True` if media is write-protected
        :raises: `error.FishyError`
        """
        device_info = self._get_device(identity, device)

        return (device_info.get('ImageName', ''),
                device_info.get('Image', ''),
                device_info.get('Inserted', False),
                device_info.get('WriteProtected', False))

    def insert_image(
        self, identity, device, image_url, inserted=True, write_protected=True
    ):
        """Upload, remove or insert virtual media

        :param identity: parent resource ID
        :param device: device name
        :param image_url: URL to ISO image to place into `device` or `None`
            to eject currently present media
        :param inserted: treat currently present media as inserted or not
        :param write_protected: prevent write access the inserted media
        :raises: `FishyError` if image can't be manipulated
        """
        MAX_CREATE_LOCAL_IMAGE_RETRIES = 5
        INITIAL_BACKOFF = 1  # in seconds
        BACKOFF_MULTIPLIER = 2

        device_info = self._get_device(identity, device)
        retries = 0
        backoff = INITIAL_BACKOFF

        while retries < MAX_CREATE_LOCAL_IMAGE_RETRIES:
            try:
                local_file_path = self._fetch_and_create_local_image(image_url)
                break  # Exit loop on success
            except (error.FishyError, OSError) as ex:
                retries += 1
                if retries < MAX_CREATE_LOCAL_IMAGE_RETRIES:
                    msg = (
                        "Attempt %d failed fetching image from URL %s: %s. Retrying after %d seconds..."
                        % (retries, image_url, ex, backoff)
                    )
                    self._logger.warning(msg)
                    time.sleep(backoff)
                    backoff *= BACKOFF_MULTIPLIER
                else:
                    msg = (
                        "Max retries reached. Failed fetching image from URL %s: %s"
                        % (image_url, ex)
                    )
                    self._logger.exception(msg)
                    raise error.FishyError(msg)

        self._logger.debug(
            "Fetched image %(file)s for %(identity)s"
            % {"identity": identity, "file": os.path.basename(local_file_path)}
        )

        device_info["Image"] = image_url
        device_info["Inserted"] = inserted
        device_info["WriteProtected"] = write_protected
        device_info["_local_file_path"] = local_file_path

        self._devices.update({(identity, device): device_info})

        return local_file_path

    def _fetch_and_create_local_image(self, image_url):
        """Fetch image from URL and create a local image file.

        :param image_url: URL to ISO image
        :returns: Path to the local file
        :raises: `FishyError` if image can't be manipulated
        """
        with tempfile.NamedTemporaryFile(mode="w+b", delete=False) as tmp_file:
            with requests.get(
                image_url,
                stream=True,
                verify=False,
                timeout=60,
            ) as rsp:
                if rsp.status_code >= 400:
                    self._logger.error(
                        "Failed fetching image from URL %s: " "got HTTP error %s:\n%s",
                        image_url,
                        rsp.status_code,
                        rsp.text,
                    )
                    target_code = 502 if rsp.status_code >= 500 else 400
                    raise error.FishyError(
                        "Cannot download virtual media: got error %s "
                        "from the server" % rsp.status_code,
                        code=target_code,
                    )

                with open(tmp_file.name, "wb") as fl:
                    for chunk in rsp.iter_content(chunk_size=8192):
                        if chunk:
                            fl.write(chunk)

                local_file = None
                content_dsp = rsp.headers.get("content-disposition")
                if content_dsp:
                    local_file = re.findall('filename="(.+)"', content_dsp)

                if local_file:
                    local_file = local_file[0]

                if not local_file:
                    parsed_url = urlparse.urlparse(image_url)
                    local_file = os.path.basename(parsed_url.path)

                if not local_file:
                    local_file = "image.iso"

                temp_dir = tempfile.mkdtemp(dir=os.path.dirname(tmp_file.name))
                local_file_path = os.path.join(temp_dir, local_file)

            os.rename(tmp_file.name, local_file_path)
        return local_file_path

    def eject_image(self, identity, device):
        """Eject virtual media image

        :param identity: parent resource ID
        :param device: device name
        :raises: `FishyError` if image can't be manipulated
        """
        device_info = self._get_device(identity, device)

        device_info['Image'] = ''
        device_info['ImageName'] = ''
        device_info['Inserted'] = False
        device_info['WriteProtected'] = False

        self._devices.update({(identity, device): device_info})

        local_file = device_info.pop('_local_file', None)
        if local_file:
            os.unlink(local_file)

            self._logger.debug(
                'Removed local file %(file)s for %(identity)s' % {
                    'identity': identity, 'file': local_file})
