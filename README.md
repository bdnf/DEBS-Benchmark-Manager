# Benchmark Docker Manager**

Run experiments on queued docker images, collected by DEBS-API Scheduler.

#### Basic usage:

Make sure all environmental variables are valid for your experiment:

You can set up them in [This .env file](server_app/.env):

  `Important!` Adjust here timeouts and absolute path for your data.

Additional variables can be set in [main docker-compose file](./docker-compose-manager.yml)

  MANAGER_SLEEP_TIME, API endpoints, and default API routes that are queried.
<br>
Run with: `docker-compose -f docker-compose-manager.yml up --build`

After API server is up.
