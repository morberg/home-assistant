"""Test Hue setup process."""
from homeassistant.setup import async_setup_component
from homeassistant.components import hue


async def test_setup_with_multiple_hosts(hass, mock_bridge):
    """Multiple hosts specified in the config file."""
    assert await async_setup_component(hass, hue.DOMAIN, {
        hue.DOMAIN: {
            hue.CONF_BRIDGES: [
                {hue.CONF_HOST: '127.0.0.1'},
                {hue.CONF_HOST: '192.168.1.10'},
            ]
        }
    })

    assert len(mock_bridge.mock_calls) == 2
    hosts = sorted(mock_call[1][0] for mock_call in mock_bridge.mock_calls)
    assert hosts == ['127.0.0.1', '192.168.1.10']
    assert len(hass.data[hue.DOMAIN]) == 2


async def test_setup_no_host(hass, aioclient_mock):
    """Check we call discovery if domain specified but no bridges."""
    aioclient_mock.get(hue.API_NUPNP, json=[])

    result = await async_setup_component(
        hass, hue.DOMAIN, {hue.DOMAIN: {}})
    assert result

    assert len(aioclient_mock.mock_calls) == 1
    assert len(hass.data[hue.DOMAIN]) == 0
