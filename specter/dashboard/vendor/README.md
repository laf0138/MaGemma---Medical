# Vendored frontend dependencies

SPECTER's dashboard has to work with zero internet access - that's the
entire premise of the project. `dashboard.html` previously loaded
Socket.IO and D3 from `cdnjs.cloudflare.com`; on a genuinely offline
network (or DNS-only/degraded RF backhaul) both `io` and `d3` came back
`undefined` and the dashboard's core script failed outright.

These two files are the same exact upstream builds vendored locally and
served by `dashboard_server.py`'s Flask static route (`/dashboard/vendor/...`):

| File | Version | Upstream | License |
|---|---|---|---|
| `socket.io.min.js` | 4.7.2 | https://github.com/socketio/socket.io-client | MIT |
| `d3.min.js` | 7.8.5 | https://github.com/d3/d3 | ISC |

Both are permissively licensed and redistributable as-is; the license
header is preserved at the top of each file. If either library is
upgraded, replace the file here and update this table - do not re-add a
CDN `<script src>` to `dashboard.html`.
