"""
This component provides basic support for the Philips Hue system.

For more details about this component, please refer to the documentation at
https://home-assistant.io/components/hue/
"""
import asyncio
import json
import ipaddress
import logging
import os

import async_timeout
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.const import CONF_FILENAME, CONF_HOST
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import discovery, aiohttp_client

REQUIREMENTS = ['aiohue==1.2.0']

_LOGGER = logging.getLogger(__name__)

DOMAIN = "hue"
SERVICE_HUE_SCENE = "hue_activate_scene"
API_NUPNP = 'https://www.meethue.com/api/nupnp'

CONF_BRIDGES = "bridges"

CONF_ALLOW_UNREACHABLE = 'allow_unreachable'
DEFAULT_ALLOW_UNREACHABLE = False

PHUE_CONFIG_FILE = 'phue.conf'

CONF_ALLOW_HUE_GROUPS = "allow_hue_groups"
DEFAULT_ALLOW_HUE_GROUPS = True

BRIDGE_CONFIG_SCHEMA = vol.Schema({
    # Validate as IP address and then convert back to a string.
    vol.Required(CONF_HOST): vol.All(ipaddress.ip_address, cv.string),
    # This is for legacy reasons and is only used for importing auth.
    vol.Optional(CONF_FILENAME, default=PHUE_CONFIG_FILE): cv.string,
    vol.Optional(CONF_ALLOW_UNREACHABLE,
                 default=DEFAULT_ALLOW_UNREACHABLE): cv.boolean,
    vol.Optional(CONF_ALLOW_HUE_GROUPS,
                 default=DEFAULT_ALLOW_HUE_GROUPS): cv.boolean,
})

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Optional(CONF_BRIDGES):
            vol.All(cv.ensure_list, [BRIDGE_CONFIG_SCHEMA]),
    }),
}, extra=vol.ALLOW_EXTRA)

ATTR_GROUP_NAME = "group_name"
ATTR_SCENE_NAME = "scene_name"
SCENE_SCHEMA = vol.Schema({
    vol.Required(ATTR_GROUP_NAME): cv.string,
    vol.Required(ATTR_SCENE_NAME): cv.string,
})


async def async_setup(hass, config):
    """Set up the Hue platform."""
    conf = config.get(DOMAIN)
    if conf is None:
        conf = {}

    hass.data[DOMAIN] = {}
    configured = _configured_hosts(hass)

    # User has configured bridges
    if CONF_BRIDGES in conf:
        bridges = conf[CONF_BRIDGES]

    # Component is part of config but no bridges specified, discover.
    elif DOMAIN in config:
        # discover from nupnp
        websession = aiohttp_client.async_get_clientsession(hass)

        async with websession.get(API_NUPNP) as req:
            hosts = await req.json()

        bridges = []
        for entry in hosts:
            # Filter out already configured hosts
            if entry['internalipaddress'] in configured:
                continue

            # Run through config schema to populate defaults
            bridges.append(BRIDGE_CONFIG_SCHEMA({
                CONF_HOST: entry['internalipaddress'],
                # Careful with using entry['id'] for other reasons. The
                # value is in lowercase but is returned uppercase from hub.
                CONF_FILENAME: '.hue_{}.conf'.format(entry['id']),
            }))
    else:
        # Component not specified in config, we're loaded via discovery
        bridges = []

    if not bridges:
        return True

    for bridge_conf in bridges:
        host = bridge_conf[CONF_HOST]

        # Store config in hass.data so the config entry can find it
        hass.data[DOMAIN][host] = bridge_conf

        # If configured, the bridge will be set up during config entry phase
        if host in configured:
            continue

        # No existing config entry found, try importing it or trigger link
        # config flow if no existing auth. Because we're inside the setup of
        # this component we'll have to use hass.async_add_job to avoid a
        # deadlock: creating a config entry will set up the component but the
        # setup would block till the entry is created!
        hass.async_add_job(hass.config_entries.flow.async_init(
            DOMAIN, source='import', data={
                'host': bridge_conf[CONF_HOST],
                'path': bridge_conf[CONF_FILENAME],
            }
        ))

    return True


async def async_setup_entry(hass, entry):
    """Set up a bridge from a config entry."""
    host = entry.data['host']
    config = hass.data[DOMAIN].get(host)

    if config is None:
        allow_unreachable = DEFAULT_ALLOW_UNREACHABLE
        allow_groups = DEFAULT_ALLOW_HUE_GROUPS
    else:
        allow_unreachable = config[CONF_ALLOW_UNREACHABLE]
        allow_groups = config[CONF_ALLOW_HUE_GROUPS]

    bridge = HueBridge(hass, entry, allow_unreachable, allow_groups)
    hass.data[DOMAIN][host] = bridge
    return await bridge.async_setup()


@callback
def _configured_hosts(hass):
    """Return a set of the configured hosts."""
    return set(entry.data['host'] for entry
               in hass.config_entries.async_entries(DOMAIN))


def _find_username_from_config(hass, filename):
    """Load username from config."""
    path = hass.config.path(filename)

    if not os.path.isfile(path):
        return None

    with open(path) as inp:
        try:
            return list(json.load(inp).values())[0]['username']
        except ValueError:
            # If we get invalid JSON
            return None


class HueBridge(object):
    """Manages a single Hue bridge."""

    def __init__(self, hass, config_entry, allow_unreachable, allow_groups):
        """Initialize the system."""
        self.config_entry = config_entry
        self.hass = hass
        self.allow_unreachable = allow_unreachable
        self.allow_groups = allow_groups
        self.available = True
        self.api = None

    async def async_setup(self, tries=0):
        """Set up a phue bridge based on host parameter."""
        host = self.config_entry.data['host']

        try:
            self.api = await _get_bridge(
                self.hass, host,
                self.config_entry.data['username']
            )
        except AuthenticationRequired:
            # usernames can become invalid if hub is reset or user removed
            # TODO: Remove self.config_entry (which we're setting up!). Start config flow.
            # Raise ConfigEntryNoAuth ?
            return False

        except CannotConnect:
            retry_delay = 2 ** (tries + 1)
            _LOGGER.error("Error connecting to the Hue bridge at %s. Retrying "
                          "in %d seconds", host, retry_delay)

            async def retry_setup(_now):
                """Retry setup."""
                if await self.async_setup(tries + 1):
                    # This feels hacky, we should find a better way to do this
                    self.config_entry.state = config_entries.ENTRY_STATE_LOADED

            # Unhandled edge case: cancel this if we discover bridge on new IP
            self.hass.helpers.event.async_call_later(retry_delay, retry_setup)

            return False

        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception('Unknown error connecting with Hue bridge at %s',
                              host)
            return False

        self.hass.async_add_job(discovery.async_load_platform(
            self.hass, 'light', DOMAIN, {'host': host}))

        self.hass.services.async_register(
            DOMAIN, SERVICE_HUE_SCENE, self.hue_activate_scene,
            schema=SCENE_SCHEMA)

        return True

    async def hue_activate_scene(self, call, updated=False):
        """Service to call directly into bridge to set scenes."""
        group_name = call.data[ATTR_GROUP_NAME]
        scene_name = call.data[ATTR_SCENE_NAME]

        group = next(
            (group for group in self.api.groups.values()
             if group.name == group_name), None)

        scene_id = next(
            (scene.id for scene in self.api.scenes.values()
             if scene.name == scene_name), None)

        # If we can't find it, fetch latest info.
        if not updated and (group is None or scene_id is None):
            await self.api.groups.update()
            await self.api.scenes.update()
            await self.hue_activate_scene(call, updated=True)
            return

        if group is None:
            _LOGGER.warning('Unable to find group %s', group_name)
            return

        if scene_id is None:
            _LOGGER.warning('Unable to find scene %s', scene_name)
            return

        await group.set_action(scene=scene_id)


@config_entries.HANDLERS.register(DOMAIN)
class HueFlowHandler(config_entries.ConfigFlowHandler):
    """Handle a Hue config flow."""

    VERSION = 1

    def __init__(self):
        """Initialize the Hue flow."""
        self.host = None

    async def async_step_init(self, user_input=None):
        """Handle a flow start."""
        from aiohue.discovery import discover_nupnp

        if user_input is not None:
            self.host = user_input['host']
            return await self.async_step_link()

        websession = aiohttp_client.async_get_clientsession(self.hass)

        try:
            with async_timeout.timeout(5):
                bridges = await discover_nupnp(websession=websession)
        except asyncio.TimeoutError:
            return self.async_abort(
                reason='discover_timeout'
            )

        if not bridges:
            return self.async_abort(
                reason='no_bridges'
            )

        # Find already configured hosts
        configured = _configured_hosts(self.hass)

        hosts = [bridge.host for bridge in bridges
                 if bridge.host not in configured]

        if not hosts:
            return self.async_abort(
                reason='all_configured'
            )

        elif len(hosts) == 1:
            self.host = hosts[0]
            return await self.async_step_link()

        return self.async_show_form(
            step_id='init',
            data_schema=vol.Schema({
                vol.Required('host'): vol.In(hosts)
            })
        )

    async def async_step_link(self, user_input=None):
        """Attempt to link with the Hue bridge.

        Given a configured host, will ask the user to press the link button
        to connect to the bridge.
        """
        errors = {}

        if user_input is not None:
            try:
                bridge = await _get_bridge(
                    self.hass, self.host, username=None
                )

                return self._entry_from_bridge(bridge)
            except AuthenticationRequired:
                errors['base'] = 'register_failed'

            except CannotConnect:
                _LOGGER.error("Error connecting to the Hue bridge at %s",
                              self.host)
                errors['base'] = 'linking'

            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception(
                    'Unknown error connecting with Hue bridge at %s',
                    self.host)
                errors['base'] = 'linking'

        return self.async_show_form(
            step_id='link',
            errors=errors,
        )

    async def async_step_discovery(self, discovery_info):
        """Handle a discovered Hue bridge.

        This flow is triggered by the discovery component. It will check if the
        host is already configured and delegate to the import step if not.
        """
        # Filter out emulated Hue
        if "HASS Bridge" in discovery_info.get('name', ''):
            return self.async_abort(reason='already_configured')

        host = discovery_info.get('host')

        if host in _configured_hosts(self.hass):
            return self.async_abort(reason='already_configured')

        # This value is based off host/description.xml and is, weirdly, missing
        # 4 characters in the middle of the serial compared to results returned
        # from the NUPNP API or when querying the bridge API for bridgeid.
        # (on first gen Hue hub)
        serial = discovery_info.get('serial')

        return await self.async_step_import({
            'host': host,
            # This format is the legacy format that Hue used for discovery
            'path': 'phue-{}.conf'.format(serial)
        })

    async def async_step_import(self, import_info):
        """Import a new bridge as a config entry.

        Will read authentication from Phue config file if available.

        This flow is triggered by `async_setup` for both configured and
        discovered bridges. Triggered for any bridge that does not have a
        config entry yet (based on host).

        This flow is also triggered by `async_step_discovery`.

        If an existing config file is found, we will validate the credentials
        and create an entry. Otherwise we will delegate to `link` step which
        will ask user to link the bridge.
        """
        host = import_info['host']
        path = import_info['path']

        username = await self.hass.async_add_job(
            _find_username_from_config, self.hass, self.hass.config.path(path))

        try:
            bridge = await _get_bridge(
                self.hass, host, username
            )

            _LOGGER.info('Imported authentication for %s from %s', host, path)

            return self._entry_from_bridge(bridge)
        except AuthenticationRequired:
            self.host = host

            _LOGGER.info('Invalid authentication for %s, requesting link.',
                         host)

            return await self.async_step_link()

        except CannotConnect:
            _LOGGER.error("Error connecting to the Hue bridge at %s", host)
            return self.async_abort(reason='cannot_connect')

        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception('Unknown error connecting with Hue bridge at %s',
                              host)
            return self.async_abort(reason='unknown')

    def _entry_from_bridge(self, bridge):
        """Return a config entry from an initialized bridge."""
        # TODO: make sure that we remove all entries of hubs with same ID
        return self.async_create_entry(
            title=bridge.config.name,
            data={
                'host': bridge.host,
                'bridge_id': bridge.config.bridgeid,
                'username': bridge.username,
            }
        )


class HueException(HomeAssistantError):
    """Base class for Hue exceptions."""


class CannotConnect(HueException):
    """Unable to connect to the bridge."""


class AuthenticationRequired(HueException):
    """Unknown error occurred."""


async def _get_bridge(hass, host, username=None):
    """Create a bridge object and verify authentication."""
    import aiohue

    bridge = aiohue.Bridge(
        host, username=username,
        websession=aiohttp_client.async_get_clientsession(hass)
    )

    try:
        with async_timeout.timeout(5):
            # Create username if we don't have one
            if not username:
                await bridge.create_user('home-assistant')
            # Initialize bridge (and validate our username)
            await bridge.initialize()

        return bridge
    except (aiohue.LinkButtonNotPressed, aiohue.Unauthorized):
        _LOGGER.warning("Connected to Hue at %s but not registered.", host)
        raise AuthenticationRequired
    except (asyncio.TimeoutError, aiohue.RequestError):
        _LOGGER.error("Error connecting to the Hue bridge at %s", host)
        raise CannotConnect
    except aiohue.AiohueException:
        _LOGGER.exception('Unknown Hue linking error occurred')
        raise AuthenticationRequired
