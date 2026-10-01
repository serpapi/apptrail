# Self-hosting AppTrail

[Back to AppTrail](../README.md)

AppTrail runs a FastAPI server and one background worker in the same process. It stores application data and queued jobs in SQLite. No Redis, Postgres, or separate job service is required.

## Install from Docker Hub

Run the [AppTrail image](https://hub.docker.com/r/serpapi/apptrail):

```bash
docker run -d --name apptrail \
  -p 80:80 \
  -v apptrail-data:/data \
  --restart unless-stopped \
  --stop-timeout 360 \
  serpapi/apptrail:latest
docker logs apptrail
```

Open [localhost](http://localhost), or `http://SERVER_IP` for a remote server. Use the one-time setup code from the logs to choose your owner username and password, then connect SerpApi. The named `apptrail-data` volume keeps your account and history when the container is replaced.

The image supports Intel/AMD and ARM Linux and uses port `80`. If that port is already in use on your server, change the mapping to `-p 8080:80` and include `:8080` in the URL. For localhost-only access, change the port mapping to `127.0.0.1:80:80`.

### CapRover and Coolify

Use the Docker Hub image `serpapi/apptrail:latest` and configure persistent storage at `/data` before the first deployment. AppTrail stores your account, history, and saved credentials there. Reusing this storage keeps your data when an update replaces the container. Run one instance.

#### CapRover

1. In **Apps**, create a new app named `apptrail` with **Has Persistent Data** checked.
2. Open the app's **App Configs** and add a directory under **Persistent Directories**:

   | Setting | Value |
   |---|---|
   | Path in App | `/data` |
   | Label / Volume Name | `apptrail-data` |
   | Set specific host path | Leave unchecked |

3. Keep **Instance Count** at `1` and click **Save & Update** to save the storage configuration.
4. Open **Deployment**, find **Deploy via ImageName**, enter `serpapi/apptrail:latest`, and click **Deploy**.

CapRover manages the volume's location on the server. See [CapRover's persistent apps guide](https://caprover.com/docs/persistent-apps) for storage details.

#### Coolify

1. Open your project and environment, select **+ New**, then choose **Docker Image**.
2. Enter `serpapi/apptrail` as **Image Name** and `latest` as **Tag**, then save to create the application.
3. Before clicking **Deploy**, open **Configuration > Persistent Storage**, select **Add > Volume Mount**, and enter:

   | Setting | Value |
   |---|---|
   | Name | `apptrail-data` |
   | Source Path | Leave empty |
   | Destination Path | `/data` |

4. Click **Add** to save the mount. Leaving **Source Path** empty lets Docker manage a named volume.
5. Configure your domain, keep the application on one server with one instance, then click **Deploy**.

See [Coolify's persistent storage guide](https://coolify.io/docs/core/persistent-storage/storage-mounts/overview) and [volume mount instructions](https://coolify.io/docs/core/persistent-storage/storage-mounts/volume-mounts) for details.

#### First setup and updates

After deploying on either platform, use the one-time setup code in the application logs to create your owner account, then connect SerpApi. If configuring a platform health check, use HTTP `/healthz`. Account setup and login work behind the platform's HTTPS proxy without an origin setting or proxy IP configuration. Read [Public HTTPS hosting](#public-https-hosting) for optional forwarded-header settings.

Before updating, open **Settings → Download SQLite backup** in AppTrail and save the SQLite database backup to your computer. Keep it so you can [restore your data](#backups-and-restoration) if the update causes issues.

Then deploy `serpapi/apptrail:latest` again in the existing CapRover app or click **Redeploy** in the existing Coolify application. Keep the same volume mounted at `/data`; do not delete or recreate it. Your saved data persists across container replacement, though the app may briefly be unavailable while restarting.

## Build from source with Docker Compose

Clone the [repository](https://github.com/serpapi/apptrail), start the service, and read its setup code:

```bash
git clone https://github.com/serpapi/apptrail.git
cd apptrail
docker compose up -d --build
docker compose logs apptrail
```

Open [localhost](http://localhost), or `http://SERVER_IP` for a remote server. Use the one-time setup code from the logs to choose your owner username and password. Connect SerpApi after signing in. The owner account is saved in SQLite, and the setup code stops working after account creation.

The included Compose file maps your server's port `80` to container port `80` on all interfaces. Allow port `80` through your server firewall for remote access. No URL environment variable or hostname allowlist is needed. For localhost-only access, change the port mapping to `127.0.0.1:80:80`.

```bash
docker compose logs -f
docker compose stop
docker compose start
docker compose down
```

The named `apptrail-data` volume survives container restarts, replacement, and ordinary `docker compose down`. `docker compose down -v` deletes the volume and its database.

## Run without Docker

```bash
uvx apptrail --host 0.0.0.0 --no-browser --port 8080
```

Or install with pip and run:

```bash
pip install apptrail
apptrail --host 0.0.0.0 --no-browser --port 8080
```

Open `http://SERVER_IP:8080` and use the setup code printed in the terminal. For a dedicated server, configure a service manager such as systemd to run `apptrail --host 0.0.0.0 --no-browser --port 8080` under a dedicated user and restart it after failures. Use an absolute executable path when starting from a virtual environment. Login is required on every interface, including localhost. Omit `--host 0.0.0.0` to keep a native launch local to the server.

## Configuration

| Option | Default | Purpose |
|---|---|---|
| `--host` | `127.0.0.1` | Network interface |
| `--port` | `0` | Free port; use a fixed value for a server |
| `--no-browser` | Disabled | Suppress automatic browser opening |
| `--data-dir` | User data directory | Override persistent storage location |
| `APPTRAIL_DATA_DIR` | User data directory | Environment alternative to `--data-dir` |
| `SERPAPI_API_KEY` | Wizard-saved key | Supply SerpApi credentials through the environment |
| `SERPAPI_KEY` | Unset | Alternate SerpApi variable |
| `FORWARDED_ALLOW_IPS` | Loopback addresses | Optional trusted proxy addresses for recognizing the original HTTPS scheme and client IP |
| `--reset-password` | Disabled | Reset the existing owner password interactively while the server is stopped |

The environment key takes precedence over a wizard-saved key. Settings shows when the environment manages it and prevents accidental replacement through the UI.

An API key entered in the wizard is stored in a permission-protected `credentials.json` file inside the data directory. It is plaintext, not encrypted. The directory is created with mode `0700` and the credential file with mode `0600` on systems supporting Unix permissions. Use environment injection from your secret manager if that better fits your deployment. Never put a live key in a Dockerfile or image.

## Public HTTPS hosting

HTTPS is optional. HTTP leaves passwords and session traffic unencrypted, so use an HTTPS reverse proxy when you need transport encryption. Account setup and login work when a proxy terminates HTTPS and forwards HTTP to AppTrail. No origin environment variable or proxy IP configuration is required for login.

To serve HTTPS, configure a reverse proxy with a valid TLS certificate and forward the original `Host`, `X-Forwarded-Proto`, and client address.

For a host-local nginx proxy, first change the Compose port mapping to `127.0.0.1:8080:80`, leaving port `80` available for the proxy. Then forward requests to AppTrail:

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $remote_addr;
    client_max_body_size 1m;
}
```

Optionally set `FORWARDED_ALLOW_IPS` to your proxy's address or subnet so AppTrail recognizes the original HTTPS scheme and client IP. This enables the cookie's `Secure` flag behind an HTTPS proxy and applies login throttling per forwarded client address. Without it, authentication still works, but AppTrail uses the connection scheme and address it sees from the proxy. Trust only your proxy's address or subnet; use `*` only when every connection reaching AppTrail comes through trusted proxies that sanitize forwarded headers. For a host-local proxy, change the Compose port mapping to `127.0.0.1:8080:80` and expose the HTTPS proxy port.

The initial setup code is printed to the server terminal or container logs. Someone who can only visit the public URL cannot create the owner account without that code. Keep access to logs and the data directory restricted. After setup, there is no public registration endpoint for additional accounts.

## Password changes and recovery

Use **Settings → Your account** to change your password. For forgotten passwords, stop the service and run `apptrail --reset-password --data-dir /path/to/data`. The prompt does not echo the new password. This preserves the account, apps, and history, and invalidates all sessions. For Docker Compose:

```bash
docker compose stop
docker compose run --rm apptrail --reset-password
docker compose up -d
```

See [Authentication](AUTHENTICATION.md) for session lifetimes, API access, and recovery details.

## Where data lives

| Platform | Default directory |
|---|---|
| macOS | `~/Library/Application Support/apptrail` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/apptrail` |
| Windows | User local application data directory under `apptrail` |
| Docker | `/data` |

The CLI prints the exact directory at startup. Pip and uvx use the same default directory for the same operating-system user. The database is `apptrail.sqlite3`.

## Scheduling and downtime

New tracking queries are queued immediately. The dispatcher checks every minute for enabled queries that are due, using daily, weekly, biweekly, or calendar-monthly intervals. Manual checks reuse any already queued or running job for that query.

Ratings and review counts in **Statistics** refresh when you select **Refresh store details** from an app’s **Manage** screen. **Listing history** has separate, optional schedules for each store, country, and language. Enable a listing there to collect snapshots automatically; each check uses a product request. Upgrades do not enable listing history or restore old automatic statistics-refresh schedules.

The worker retries transient failures up to three attempts with a delay. Final errors remain in history and do not become a “not found” ranking. Regular checks resume at the configured interval. Restarting recovers interrupted jobs; a request interrupted after it reached SerpApi may be repeated, so exact-once billing cannot be guaranteed.

The browser can close while tracking continues. If the process stops or the computer sleeps, checks pause. On restart, AppTrail queues current checks for overdue queries. It cannot recover past results from the time it was offline. Graceful shutdown waits for the current provider operation to finish; individual requests have a 90-second timeout. Compose allows six minutes for shutdown. Use `--stop-timeout 360` with `docker run` as well, since a Google Play check can fetch three pages. The CLI stops waiting for stalled HTTP responses after ten seconds before shutting down the worker.

## Storage usage and cleanup

In **Settings → Storage usage**, check the total database size and the space used by saved SerpApi responses and listing-history images. Each category has a one-time cleanup option for content older than **7 days** or **30 days**, with an estimate of the content eligible for deletion. Review the confirmation before deleting. Rankings, saved answers, matched evidence, listing text and image-change records are kept. Images shared with snapshots inside the selected period are also kept.

Cleanup waits for active checks, deletes the selected older content, then compacts the database to return space to disk. Other requests may briefly show that cleanup is in progress. The history views explain when original responses or archived images were deleted. New checks continue saving both; cleanup does not set an automatic retention policy. The total includes SQLite's temporary journal but excludes separate backup files, which cleanup leaves untouched. Compaction needs temporary free disk space; if it cannot finish, AppTrail reports that the deleted pages remain available for SQLite to reuse.

## Backups and restoration

Use **Settings → Download SQLite backup** to create a consistent snapshot while the app is running. The backup includes the owner account and password hash, apps, competitors, queries, settings, jobs, account usage snapshots, saved rankings and answers, matched evidence, and listing text history. Keep it private. It excludes raw SerpApi responses, archived listing images, login sessions, `credentials.json`, and environment secrets. AppTrail compacts the copy after removing those records so the downloaded file uses less space. The running database is unchanged. **Export history CSV** downloads search results for spreadsheet analysis; CSV files cannot restore a workspace.

Restored search details explain that original response data was omitted to save space. Listing comparisons show placeholders for omitted images and keep their hashes and change records, so future checks can still detect changes. New checks save responses and images normally. AppTrail does not fetch live images as substitutes for missing historical images. Automatic recovery and schema-upgrade backups preserve all available response data and images.

Use **Settings → Restore from SQLite** to upload a compatible AppTrail backup of up to 2 GB. Schema versions 6 and 7 are supported; schema 6 is validated and upgraded in a temporary copy before restoration. Confirm the overwrite, then select **Overwrite and restore**. Invalid uploads leave your workspace and login intact.

On a fresh installation without an owner account, select **Have a backup? Restore your data** below **Create account**. Use the setup code from the new server's terminal or container logs, then upload your backup. You can restore before creating another account and sign in with the credentials saved in the backup.

Restoring overwrites all current server data, including the owner account, password, apps, settings, and history. AppTrail waits for active requests and background checks to finish, saves the current database in `backups/before-restore-*.sqlite3`, then replaces it. All sessions are revoked, including sessions in manually copied backups. Sign in with the username and password saved in the uploaded backup. The current server's SerpApi key is kept because it is stored outside SQLite. Restored schedules resume with that key.

To restore a larger backup, upgrade an older backup through the startup migrations, or restore without signing in:

1. Stop every AppTrail process using the destination directory.
2. Move the existing data directory aside as a rollback copy.
3. Create a fresh data directory and put the downloaded file there as `apptrail.sqlite3`.
4. Set ownership and permissions for the AppTrail user, then start with `--data-dir` pointing to that directory.
5. Sign in with the restored owner account, then supply your SerpApi key through the environment or reconnect in Settings.

For Docker, perform the same operation inside the named volume with the service stopped. The container runs as UID/GID `10001`. Copy the database into an empty destination; do not retain old `-wal` or `-shm` files alongside a restored database.

## Updates

Before any update, use **Settings → Download SQLite backup** in AppTrail to save a SQLite database backup to your computer. Keep it so you can [restore your data](#backups-and-restoration) if the update causes issues.

For an installation from Docker Hub, pull the new image and replace the container, reusing its data volume:

```bash
docker pull serpapi/apptrail:latest
docker stop apptrail
docker rm apptrail
docker run -d --name apptrail \
  -p 80:80 \
  -v apptrail-data:/data \
  --restart unless-stopped \
  --stop-timeout 360 \
  serpapi/apptrail:latest
```

Use the same port mapping, environment settings, and volume as your original container. If you pinned a release, use the new version's tag in the pull and run commands.

For a source build with Docker Compose, update the checkout and run `docker compose up -d --build`. For pip, use `pip install --upgrade apptrail`; for uvx, use `uvx apptrail@latest`. The server applies numbered schema migrations at startup and refuses databases created by a newer unsupported schema.

Allow extra disk space for upgrade backups, copied data, and SQLite's write-ahead log. The schema 4 upgrade separates saved response bodies from run metadata; freed pages remain available for SQLite to reuse, so the database file may not shrink afterward. Let the upgrade finish before stopping the process.
