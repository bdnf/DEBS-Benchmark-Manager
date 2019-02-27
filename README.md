*** Benchmark Docker Manager ***
Run experiments on queued docker images, collected by DEBS-API Scheduler.
<br>
<br>####Basic usage:
<br>Make sure all environmental variables are valid for your experiment:
<br>You can set up them in [This .env file](server_app/.env):
<br>...`Important!` Adjust here timeouts and absolute path for your data.
<br>
<br>#####Additional variables can be set in [main docker-compose file](./docker-compose-manager.yml)
<br>...MANAGER_SLEEP_TIME, API endpoints, and default API routes that are queried.
<br>
<br>
<br> ####Run with: `docker-compose -f docker-compose-manager.yml up --build`
<br> After API server is reachable.
