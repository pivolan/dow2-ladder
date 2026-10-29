# TCP on the game servers (Moscow, iggy, polsha)

DoW2 sends the turns over TCP. A lost packet holds the whole connection until it is sent again; Linux waits at least
200 ms for that, so every loss is a short lag for the whole match. On every game server:

- `dow2-rto-min` sets a 40 ms minimum retransmission timeout on the default route (`ip route change ... rto_min 40ms`);
- `dow2-rto-min.timer` puts it back every 5 minutes (a DHCP renewal brings the route back without it) and after a boot.

Installed by hand (29.10.2026): copy the script to /usr/local/bin, the units to /etc/systemd/system,
`systemctl enable --now dow2-rto-min.timer`. battle_relay.py itself sets thin-stream TCP and immediate acks per connection.
