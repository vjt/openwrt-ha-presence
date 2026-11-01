# eve-push (OpenWrt AP side)

Pushes hostapd station events (`sta-authorized` → `assoc`, `disassoc`) to
eve's `POST /event` so arrivals and roams publish at once instead of at the
next scrape. Needs only what a stock OpenWrt 25.12 image ships: `ucode`,
`ucode-mod-ubus`, `ucode-mod-uloop`, `ucode-mod-log`, `uclient-fetch`.
Design and the hostapd/usteer constraints: `docs/plans/2026-11-01-ap-push-events.md`.

Per AP (the hostname must be the `nodes:` key in eve's config):

```sh
openssl rand -hex 32          # on eve's host → nodes.<ap>.token in config.yaml
scp eve-push      root@<ap>:/usr/bin/eve-push
scp eve-push.init root@<ap>:/etc/init.d/eve-push
ssh root@<ap> '
  chmod 755 /usr/bin/eve-push /etc/init.d/eve-push
  echo http://<eve-ip>:<event_port>/event > /etc/eve-push.url
  umask 077; echo <token> > /etc/eve-push.token
  for f in /usr/bin/eve-push /etc/init.d/eve-push /etc/eve-push.url /etc/eve-push.token; do
    grep -qxF $f /etc/sysupgrade.conf || echo $f >> /etc/sysupgrade.conf
  done
  /etc/init.d/eve-push enable && /etc/init.d/eve-push start'
```

No wifi restart is needed: the daemon subscribes to the running
`hostapd.*` objects and to new ones as they appear. Check with
`logread -e eve-push` on the AP and `ap_event` lines in eve's log.
