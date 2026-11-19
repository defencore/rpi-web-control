# Ethernet and Wi-Fi

Open **Network** to see the interfaces and IPv4 addresses on the Pi. Under **Connection settings**, select the interface by its reported name and choose its operating mode. A wired-only CM5 can configure Ethernet. Station mode requires a managed Wi-Fi radio. To host an access point, enable the radio, configure its regulatory country in Raspberry Pi OS and choose an adapter with AP support.

| Connection | When to use it | Where the Pi's IPv4 address belongs |
| --- | --- | --- |
| Ethernet | Connect the Pi to a wired LAN or directly to a workstation. | Ethernet interface |
| Wi-Fi station | Join a Wi-Fi network using its SSID and WPA-Personal password. | Wi-Fi interface |
| Wi-Fi access point | Let clients connect directly to the Pi's protected SSID. | Wi-Fi interface |
| Ethernet + Wi-Fi access point bridge | Put clients on the cable and clients on the Pi's SSID on one LAN. | Bridge interface `br-rpi` |

## Create a Wi-Fi access point

1. Open the console over Ethernet. In **Network → Wi-Fi access point**, choose **Enable Wi-Fi** if the radio is switched off. If several radios support AP mode, select the **Access point interface**. Choose **Set up access point** for a new network or **Edit access point** for an existing standalone AP.
2. Use the proposed network name (`RPi-Camera`) or enter your own. Enter a WPA password of 8–63 printable ASCII characters, or a 64-digit hexadecimal key. **Local DHCP server** gives each phone or laptop an address automatically. Keep the suggested Pi address unless you need another unused subnet. A typical address is `10.42.0.1/24`.
3. Select **Start access point**. On your phone or laptop, join the SSID using that password. If the client asks, stay connected to the network without internet. Open the exact console URL shown under **Wi-Fi access point**, for example `https://10.42.0.1:8080`. Choose **Keep settings** through this connection within 120 seconds to save the profile. The console works offline.

If you cannot reach the console over Wi-Fi, keep the cable connected. Choose **Roll back** over Ethernet or let the trial expire. The standalone AP uses the Wi-Fi interface and its own subnet. To put both links on the same LAN and use one address, see [bridge setup](#use-the-same-lan-over-cable-and-wi-fi).

### If the access point is unavailable

| What the console shows | What to check |
| --- | --- |
| Wi-Fi switched off | Select **Enable Wi-Fi** and allow NetworkManager a few seconds to bring up the radio. |
| Hardware block | Check the adapter's physical radio switch, if fitted, and inspect `rfkill list` on the Pi. |
| Adapter unavailable after enabling Wi-Fi | Set the country where the Pi is used in `sudo raspi-config` under **Localisation Options → WLAN Country**. Check `nmcli radio` and `nmcli device status`. [Raspberry Pi wireless setup](https://www.raspberrypi.com/documentation/computers/configuration.html) |
| No Wi-Fi adapter or no AP capability | Check that Raspberry Pi OS recognizes the adapter and its driver supports AP mode. Some CM5 variants omit the wireless radio. “Lite” describes eMMC storage. An AP-capable USB Wi-Fi adapter is another option. [Compute Module variants](https://www.raspberrypi.com/documentation/computers/compute-module.html) |

## Connect by Ethernet when the main address is unknown

The installed service provides a secondary management address, `192.168.55.100/24` by default. It runs independently of NetworkManager, the saved network profiles and DHCP. A direct cable connection works offline.

1. Connect the workstation's Ethernet port directly to the Pi's recovery port. On a board with one physical Ethernet port, use that port.
2. Set the workstation's wired IPv4 address to `192.168.55.10` with prefix `24` (mask `255.255.255.0`). Leave the gateway and DNS empty. If the workstation also has a route to this subnet over Wi-Fi or a VPN, ensure traffic for the Pi uses the cable.
3. Allow a timer cycle (about five seconds) plus time for the address-conflict check. Open `https://192.168.55.100:8080`. If SSH is enabled, use your board login, for example `ssh ubuntu@192.168.55.100`. The console and SSH each use their usual credentials.
4. Open **Network** to inspect the primary configuration. When applying a change, reconnect over the intended primary address and interface before selecting **Keep settings**. Reaching the recovery address alone verifies only the recovery connection. An unconfirmed trial still expires after 120 seconds.

The recovery address uses one enabled physical Ethernet port with carrier. Automatic selection prefers `eth0`, then `end0`, then the first physical Ethernet name in alphabetical order. When that port belongs to the appliance's `br-rpi` bridge, the bridge owns the address and Wi-Fi clients on its LAN may also reach it. A separate Wi-Fi station and administrator-managed bridge are outside the recovery service's scope. Give the workstation its own address on the recovery subnet.

### Change the recovery address or port

Edit these assignments in `/etc/default/rpi-web-control` on the Pi. To use another port, replace `auto` with its physical interface name, for example `eth1`. Set the address to another unused IPv4 address with its prefix, or to `off` to disable the extra address.

```ini
RPI_CONTROL_RECOVERY_ADDRESS=192.168.55.100/24
RPI_CONTROL_RECOVERY_INTERFACE=auto
```

The next run of `rpi-web-control-recovery.timer` reads the file. To apply it immediately, run `sudo systemctl start rpi-web-control-recovery.service`. After changing the subnet, update the workstation's address and console URL accordingly.

Reserve the recovery address for the Pi and exclude it from DHCP allocation on any shared LAN. This includes a LAN where the Pi's own **Local DHCP server** is running. If that server uses a different address for the Pi and its pool cannot exclude the recovery address, use a separate recovery subnet. If another host answers for the address, the service reports a conflict and skips assignment. Use a direct, isolated cable or choose a free address. When the primary configuration already supplies the recovery IP on the selected interface, the service leaves that configuration in place.

**Network → Wired recovery** displays the address and its status, also available as `network.recovery` in `GET /api/status`. An active address is supplied by the recovery service. A native address is supplied by the existing network configuration. Waiting, conflict, disabled and error states explain why the service has not assigned an address. To investigate locally, run `systemctl status rpi-web-control-recovery.timer` and `journalctl -b -u rpi-web-control-recovery.service -n 20`.

## Give a wired Pi a fixed address

1. Select the managed **Interface**, for example `eth0`, and choose **Ethernet** as the **Operating mode**. For automatic addressing, choose **DHCP client** to obtain the Pi's IPv4 address from the LAN's DHCP server. An optional DNS override replaces the servers supplied by DHCP.
2. For the fixed address in this example, select **Static address** and enter `192.168.55.100/24`. Check that the address is unused and reserved for the Pi on this LAN. If the Pi needs to reach other networks or resolve hostnames, supply the LAN's gateway and DNS server addresses. Leave those fields empty for a direct, offline connection.
3. Select **Apply temporarily**. On a direct cable without DHCP, give the workstation another unused address on the subnet, for example `192.168.55.10/24`. Open `https://192.168.55.100:8080` and use **Keep settings** to confirm.

## Use the same LAN over cable and Wi-Fi

Connect the Ethernet cable to a live port before testing a bridge. Select the wired interface and choose **Ethernet + Wi-Fi bridge**, then select a separate AP-capable Wi-Fi interface. Enter an SSID and WPA-Personal password for the Pi's access point. The Pi places both interfaces in `br-rpi` and uses the bridge's IPv4 address for the console. A computer on Ethernet and a phone on the Pi's SSID can reach the same address.

| LAN | Bridge IPv4 settings | How clients get addresses |
| --- | --- | --- |
| Existing LAN, fixed Pi address | **Static address** with `192.168.55.100/24` and the LAN gateway and DNS if needed. | The existing LAN DHCP server supplies wired and wireless clients. |
| Existing LAN, automatically assigned Pi address | **DHCP client** | The existing LAN DHCP server supplies the Pi and its clients. |
| Isolated LAN, fixed client addresses | **Static address** with `192.168.55.100/24` and empty gateway and DNS | Assign each client a unique address such as `192.168.55.10/24`. |
| Isolated LAN, Pi supplies addresses | **Local DHCP server** with the Pi at `192.168.55.100/24`. | NetworkManager runs a DHCP server for clients on both bridge ports. |

Only enable the Pi's DHCP server on an isolated LAN where it is intended to assign client addresses. Connecting a bridge that serves DHCP to a LAN with another DHCP server can give clients conflicting gateway or DNS settings. Shared mode also provides DNS forwarding and can use NAT through an available uplink. The local console works without internet access. [NetworkManager IPv4 settings](https://www.networkmanager.dev/docs/api/latest/nm-settings-nmcli.html)

## Confirm or recover a change

Network changes can move the console to another address or disconnect the browser. Note the current address before applying. When testing a station profile, join the target LAN. When testing an AP, join its SSID. For a static address, open the Pi at the address you entered. For DHCP, look up the Pi in the router's lease list or use its hostname if multicast DNS works on the new LAN. With token access enabled, a new address may prompt for sign-in.

After activation, you have 120 seconds to confirm. Check that the console responds over the intended connection. In the pending-change banner on any page, choose **Keep settings** before the countdown expires or **Roll back** to restore the previous settings. For a bridge, test the same console address from a wired client and an AP client. If the new configuration is unreachable, allow the rollback timer to expire, reconnect the client to its previous network and reopen the original address. NetworkManager handles timed recovery on the board even if the browser is closed. Keep network adapters connected during a trial. When the first bridge is created, rollback can disconnect devices plugged in after staging. [NetworkManager checkpoint API](https://networkmanager.dev/docs/api/latest/gdbus-org.freedesktop.NetworkManager.html)

## API

Send `POST /api/network` with a JSON body. This example stages the standalone Ethernet configuration for the offline subnet.

```json
{
  "action": "apply",
  "mode": "ethernet",
  "interface": "eth0",
  "ipv4_method": "manual",
  "address": "192.168.55.100/24",
  "gateway": "",
  "dns": []
}
```

For a bridge, use `mode: "bridge"`, the Ethernet name in `interface`, and the AP radio name in `wifi_interface`. Supply `ssid` and `password` for `bridge`, `ap` and `station`. Choose `ipv4_method` according to the intended role.

| Value | Modes | Other IPv4 fields |
| --- | --- | --- |
| `auto` | Ethernet, station, bridge | Empty address and gateway. An optional DNS list overrides DHCP-provided servers. |
| `manual` | Ethernet, station, AP, bridge | Address with CIDR prefix required. Gateway and DNS list optional. |
| `shared` | Ethernet, AP, bridge | Supply a CIDR address, empty gateway and empty DNS list. A standalone AP may leave the address empty for a NetworkManager-selected subnet. |

Read `network.wifi_radio` in `GET /api/status` to distinguish NetworkManager's software setting (`enabled`) from hardware availability (`hardware_enabled`). When software Wi-Fi is off and hardware is available, post `{"action":"enable_wifi"}` to `/api/network` and wait for the managed radio to become ready before applying an AP profile.

The response includes `pending.id` and `pending.remaining_s` (seconds). After reconnecting, read the same fields under `network.pending` in `GET /api/status`. To keep the trial, post `{"action":"confirm","id":"<pending.id>"}` to `/api/network`. To cancel it immediately, use `"action":"rollback"` with the same ID. A completed change clears `pending`. A conflicting trial or stale confirmation ID returns HTTP 409. The legacy four-field Wi-Fi request (`interface`, `mode`, `ssid`, `password`) is committed on successful activation for compatibility.

## Service and credentials

The web service delegates configuration to the group-restricted socket at `/run/rpi-web-control-network.sock`. The helper validates network requests and applies profiles through NetworkManager D-Bus. Use the console over HTTPS when supplying a Wi-Fi password. Confirmation saves the candidate profiles and retires the affected application-managed profiles. Administrator-created profiles are preserved. NetworkManager stores the secret with the saved system profile. The application omits it from status, configuration files, command arguments and logs.

Install NetworkManager and Debian's `python3-dbus` for network control. Shared IPv4 also uses `dnsmasq-base`. Inspect an installed Pi with `nmcli device status`, `ip -brief address`, `ip link show master br-rpi` and `systemctl status rpi-web-control-network.socket`. The installer can collect a private diagnostic report from the board. [Package guide](package.md#collect-diagnostics)
