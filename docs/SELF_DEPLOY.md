# Self-deploy on OCI ARM64

Alpha Cockpit deploys only after the CI workflow succeeds on main.

## Deployment chain

~~~text
merge main
   |
   v
CI
- compose validation
- app image build
- unit tests
- API smoke test
- multi-arch image publish
   |
   v
GHCR
ghcr.io/ngtrthanh/alpha-cockpit:sha-<commit>
   |
   v
Deploy OCI workflow
(self-hosted ARM64 runner)
   |
   v
scripts/deploy.sh
- lock
- verify commit belongs to main
- reject stale deploy
- online SQLite backup
- pull exact commit image
- compose up --no-build
- app commit health gate
- worker heartbeat gate
- automatic rollback on failure
~~~

The deployment does not rebuild application containers on OCI during the normal path. OCI pulls the exact multi-arch image published by CI for that commit.

Hermes is intentionally not automatically pulled on every application deploy. nousresearch/hermes-agent:stable therefore does not change unexpectedly during an Alpha Cockpit release. Upgrade Hermes separately and deliberately.

## One-time OCI runner setup

Use a dedicated non-root Linux user that can run Docker without sudo.

Required commands on the server:

~~~bash
git --version
docker version
docker compose version
curl --version
python3 --version
flock --version
~~~

In GitHub open:

~~~text
Repository
-> Settings
-> Actions
-> Runners
-> New self-hosted runner
~~~

Choose Linux / ARM64 and follow the generated installation commands on the OCI server.

When configuring the runner, add this custom label:

~~~text
alpha-cockpit
~~~

The deploy workflow requires all labels:

~~~text
self-hosted
linux
ARM64
alpha-cockpit
~~~

Install the runner as a service using the commands GitHub gives for the downloaded runner, then verify it shows Idle in repository settings.

## Production directory

The runner workspace is disposable. Production state must live outside it.

Recommended:

~~~bash
mkdir -p "$HOME/alpha-cockpit-prod"
git clone https://github.com/ngtrthanh/alpha-cockpit.git "$HOME/alpha-cockpit-prod"
cd "$HOME/alpha-cockpit-prod"

cp .env.example .env
mkdir -p data hermes-data
~~~

Configure .env with the real Hermes/SSI/model settings.

If production currently lives somewhere else, create this GitHub Actions repository variable:

~~~text
ALPHA_DEPLOY_DIR=/absolute/path/to/current/alpha-cockpit
~~~

Location:

~~~text
Settings -> Secrets and variables -> Actions -> Variables
~~~

If the variable is absent, deployment defaults to:

~~~text
$HOME/alpha-cockpit-prod
~~~

where $HOME belongs to the self-hosted runner service account.

## First bootstrap

Before enabling unattended deployment, the production directory must already contain:

- .env
- data/
- hermes-data/
- valid Hermes configuration/authentication

The deploy script refuses to proceed if .env is absent.

Run the stack once manually if necessary:

~~~bash
cd "$HOME/alpha-cockpit-prod"
docker compose pull hermes
docker compose up -d --build
curl -fsS http://127.0.0.1:6868/api/health
~~~

## Normal deployment

No SSH action or remote Docker socket is used.

After a successful CI run on main, GitHub schedules Deploy OCI on the dedicated OCI runner. The runner:

1. verifies the target SHA is reachable from origin/main;
2. skips an older delayed workflow event if a newer healthy commit is already deployed;
3. creates a SQLite online backup using the running application container;
4. checks out the exact target SHA in the production directory;
5. pulls ghcr.io/ngtrthanh/alpha-cockpit:sha-<SHA>;
6. starts Compose with --no-build;
7. waits for /api/health to report the exact SHA;
8. requires the worker heartbeat to be ok.

## Rollback

If the health gate fails:

1. recent service logs are printed into the Actions job;
2. the worker/app are stopped;
3. the pre-deploy SQLite backup is restored;
4. Git is reset to the previous commit;
5. the exact image that was running before deployment is reused;
6. legacy pre-self-deploy versions are accepted by the rollback health check even if they do not expose a commit SHA.

A failed deployment remains a failed GitHub job even when rollback succeeds. This makes failures visible while restoring service.

Backups are stored under:

~~~text
data/backups/
~~~

and are gitignored.

## Manual deployment

Deploy OCI also supports workflow_dispatch.

A SHA may be supplied explicitly. The deploy script still refuses a commit that is not reachable from main.

## Security boundaries

- GitHub's ephemeral GITHUB_TOKEN is used to pull the CI image from GHCR.
- .env, hermes-data/, SQLite databases and backups are never committed.
- No SSH private key is stored in GitHub because deployment executes locally on the OCI runner.
- No Docker socket is exposed over TCP.
- Broker write access remains absent.
