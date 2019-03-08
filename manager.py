import docker
import logging
import subprocess
import datetime
import sys, os
import yaml
import json
import re
from collections import OrderedDict
import dataset
import time
import requests
#mysql connector
import pymysql
pymysql.install_as_MySQLdb()

SPLIT_PART = 0 # !!! of string part of dockerhub image_name.split("/")[SPLIT_PART]
# 1) for testing withing multiple containers in one docker repo
# 0) for running over multiple docker repos

# HOST = "http://127.0.0.1:8080"
SCHEDULE_PATH = os.getenv("API_SCHEDULE_PATH", default= '/schedule')
RESULT_PATH = os.getenv("API_RESULT_PATH", default='/result')
STATUS_PATH = "/status_update"
MAX_RETRY_ATTEMPTS = 3

LOG_FOLDER_NAME = "manager_logs"
if not os.path.exists(LOG_FOLDER_NAME):
    os.makedirs(LOG_FOLDER_NAME)
filename = 'compose_manager.log'
logger = logging.getLogger()
logging.basicConfig(
                    level=logging.DEBUG,
                    format='%(asctime)s - %(name)s - %(threadName)s -  %(levelname)s - %(message)s',
                    handlers=[
                     logging.FileHandler("%s/%s" % (LOG_FOLDER_NAME, filename)),
                     #logging.StreamHandler().setLevel(logging.INFO)
                    ])
formatter = logging.Formatter('%(asctime)s - %(name)s - %(threadName)s -  %(levelname)s - %(message)s')
stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.INFO)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

endpoint = os.getenv("API_SERVER")
if endpoint is None:
    logging.error("please specify front-end server address!")
    # exit(1)
    # endpoint = HOST # default endpoint for local runs

# if Mac OS and running on the same machine with DEBS-Api
# specify (API_SERVER: host.docker.internal)

if "docker" in endpoint:
    endpoint = 'http://' + endpoint + ":8080"

logging.debug("API endpoint %s" % endpoint)


class Manager:

    def __init__(self):
        # used to encode datetime objects
        json.JSONEncoder.default = lambda self,obj: (obj.isoformat() if isinstance(obj, datetime.datetime) else None)
        self.images = []
        self.retry_attempts = {} #for each image
        self.client_progress_status = 0 #how many scenes processed until now

    # not used but might be helpful
    def find_container_ip_addr(self, container_name):
        info = subprocess.check_output(['docker', 'inspect', container_name])
        # parsing nested json from docker inspect
        ip = list(json.loads(info.decode('utf-8'))[0]["NetworkSettings"]["Networks"].values())[0]["IPAddress"]
        print("%s container ip is: %s" % (container_name, ip))
        return ip

    def execute(self, cmd):
        # prints command line output into console in real-time
        popen = subprocess.Popen(cmd, stdout=subprocess.PIPE, universal_newlines=True)
        for stdout_line in iter(popen.stdout.readline, ""):
            yield stdout_line
        popen.stdout.close()
        return_code = popen.wait()
        self.benchmark_return_code = return_code
        if return_code:
            #raise subprocess.CalledProcessError(return_code, cmd)
            logging.info("Docker-compose done executing with code %s" % return_code )


    def create_docker_compose_file(self, image, container):
        # using mock_file way more accurate
        mock_file = "docker-compose-mock.yml"
        logging.info("creating docker-compose with image: %s and container: %s " % (image, container))
        with open(mock_file) as f:
            list_doc = yaml.safe_load(f)
        list_doc["services"]["client"]["container_name"] = container
        list_doc["services"]["client"]["image"] = image

        volumes = list_doc["services"]["server"]["volumes"]
        log_volume = volumes[1].split(":")

        log_volume = log_volume[0]+ "/" + str(image.split("/")[SPLIT_PART]) +":"+log_volume[1]
        new_volumes = [volumes[0],log_volume]
        list_doc["services"]["server"]["volumes"] = new_volumes

        new_name = "docker-compose.yml" #str(split_name[0]) + str(file_number) + "."+ str(split_name[1])

        with open(new_name, "w") as f:
            yaml.dump(list_doc, f, default_flow_style=False)
        logging.info("docker compose file saved with name %s" % new_name)

    def get_images(self):
        # requests schedule
        global endpoint
        updated_images = []
        response = requests.get(endpoint + SCHEDULE_PATH)
        logging.info("Scheduler answer status %s " % response.status_code)
        if (response.status_code == 403):
            logging.error("Manger can't access remote server. FORBIDDEN %s " % response.status_code)
        try:
            images = response.json()

        except json.decoder.JSONDecodeError as e:
                logging.info(" Check if the front-end server is reachable! Cannot retrieve JSON response.")
                logging.error(" Got error %s " % e)
                images = {}
                pass
                # exit(1)

        for image, status in images.items():
            if status == 'updated':
                try:
                    docker_hub_link = image.split('/')
                    updated_images.append(image)
                    self.post_status({image:"Queued"})
                except IndexError:
                    logging.error('Incorrectly specified image encountered. Format is {team_repo/team_image}')
                    continue
        return updated_images

    def save_container_log(self, cmd, logfile, extension):
        dir = "../logs/" + logfile.split('/')[SPLIT_PART]
        if not os.path.exists(dir):
            os.makedirs(dir)
        with open(dir + "/" + logfile.split('/')[SPLIT_PART] + extension, "w+") as f:
            p = subprocess.Popen(cmd, shell=True, universal_newlines=True, stdout=f)
            p.wait()

    def process_result(self, docker_img_name, image_tag):
            global loop_time
            logging.info("Running image: %s " % docker_img_name)
            logging.info("Extracting results")
            team_result = self.extract_result_files(docker_img_name)
            if team_result:
                team_result['tag'] = image_tag
                team_result['last_run'] = datetime.datetime.utcnow().replace(microsecond=0).replace(second=0)
                team_result['piggybacked_manager_timeout'] = loop_time
                logging.info("Sending results: %s" % team_result)
                self.client_progress_status = team_result.get("computed_scenes",0)
                return team_result
            else:
                logging.error("No results after becnhmark run of %s" % docker_img_name)
                return {'team_image_name': docker_img_name,'computed_scenes':0}
            sys.stdout.flush()

    def extract_result_files(self, full_image_name):
        logging.info("Looking for log folders")
        rootdir = "./logs"
        if "logs" in os.walk(rootdir):
            pass
        else:
            rootdir = "../logs"

        dir = full_image_name.split('/')[SPLIT_PART]
        list_of_files = os.listdir(rootdir+"/"+dir)
        # print("files", list_of_files)
        list_of_files = [i for i in list_of_files if ".json" in i]
        if not list_of_files:
            logging.warning('No file result.json yet')
            return {}
        fresh_log = list_of_files[0]
        # print(fresh_log)
        res_json_folder = rootdir + "/"+ dir + "/"
        new_log = fresh_log.split('.')[0] + "checkedAt" + datetime.datetime.utcnow().strftime("%s") + "."+ fresh_log.split('.')[1]
        with open(rootdir + "/"+ dir + "/" + fresh_log) as f:
            data = json.load(f)
            data['team_image_name'] = full_image_name
            logging.info("Found data in %s is: %s" % (dir, data))
        subprocess.check_output(['mv', res_json_folder+fresh_log, res_json_folder+new_log])
        logging.info("Removed result file :%s after check" % res_json_folder+new_log)
        return data

    def start(self):
        logging.info("----------------------------")
        logging.info("Benchmark Manager started...")
        benchmark_container_name = "benchmark-server-logging"
        client_container_name = "client-app-"

        # requesting schedule
        images = self.get_images()

        try:
            subprocess.Popen(['docker', 'stop', benchmark_container_name], stderr=subprocess.PIPE)
            subprocess.Popen(['docker', 'rm', benchmark_container_name], stderr=subprocess.PIPE)

        except subprocess.CalledProcessError as e:
            logging.debug("Cleaning up unused containers, if they are left")
            logging.debug("Got cleanup error: %s. Proceeding!" % e)
            pass

        logging.info("Current scheduled images: %s" % images)
        time.sleep(5) # not necessary but if manager rerun, sometimes first image
                      # might be too slow to establish a connection

        for docker_img_name in images:
            try:
                subprocess.check_output(['docker', 'rm', client_container_name+docker_img_name.split("/")[SPLIT_PART]])
            except Exception as e:
                logging.debug("Cleaning up unused client containers, if they are left")
                logging.debug("Got client cleanup error: %s. Proceeding!" % e)
                pass

            tag = ""
            try:
                logging.info("Pulling image ........... %s" % docker_img_name)
                self.post_status({docker_img_name: "Pulling image"})

                subprocess.check_output(['docker', 'pull', docker_img_name])
                tag = subprocess.check_output(['docker', 'inspect', docker_img_name])
                tag = json.loads(tag.decode('utf-8'))[0]["Id"]
                logging.debug("Image tag is : %s" % tag)
            except Exception as e:
                #logging.error("Error during pull happened %s" % e)
                logging.error("Probably can't access image: %s. Error %s" % (docker_img_name, e))
                continue

            container_name = client_container_name+docker_img_name.split("/")[SPLIT_PART]
            self.create_docker_compose_file(docker_img_name, container_name) #TODO change for [0] for client repo name

            self.post_status({docker_img_name: "Running experiment"})

            cmd = ['docker-compose', 'up', '--build', '--abort-on-container-exit']
            # real-time output
            for path in self.execute(cmd):
                # print(path, "")
                logging.info(path)
                sys.stdout.flush()


            logging.debug("Docker-compose exited")
            self.post_status({docker_img_name: "Preparing results"})

            client_container = client_container_name+docker_img_name.split("/")[SPLIT_PART]

            cmd1 = 'docker logs ' + client_container
            filename = docker_img_name
            self.save_container_log(cmd1, filename, '_client_container.log')
            cmd2 = 'docker logs ' + benchmark_container_name
            self.save_container_log(cmd2, filename, '_bench_container.log')

            logging.debug("Container logs saved")
            logging.info("Image %s completed " % docker_img_name)
            team_result = self.process_result(docker_img_name, tag)

            if self.benchmark_return_code and self.client_progress_status == 0:
                logging.error("Docker-compose exited with code %s" % self.benchmark_return_code)
                logging.warning("Will retry on the next run")
                if self.retry_attempts.get(docker_img_name,0) <= MAX_RETRY_ATTEMPTS:
                     self.retry_attempts[docker_img_name] = self.retry_attempts.get(docker_img_name,0) + 1
                     self.post_status({docker_img_name: "Retrying"})
                     continue
                else:
                    self.retry_attempts[docker_img_name] = self.retry_attempt.get(docker_img_name,0)
                    pass

            logging.info("retry dict is: %s " % self.retry_attempts)

            self.post_status({docker_img_name: "Ready"})
            self.post_result(team_result)

            logging.info("Completed run for %s" % docker_img_name)

        logging.info("Evaluation completed.")
        images = []
        return

    def post_result(self, payload):
        global endpoint
        headers = {'Content-type': 'application/json'}
        try:
            response = requests.post(endpoint + RESULT_PATH, json = payload, headers=headers)

            if (response.status_code == 201):
                return {'status': 'success', 'message': 'updated'}
            if (response.status_code == 404):
                return {'message': 'Something went wrong. No scene exist. Check if the path is correct'}
        except requests.exceptions.ConnectionError as e:
            logging.error("Check if the front-end server address known! or", e)
            pass

    def post_status(self, payload):
        headers = {'Content-type': 'application/json'}
        try:
            response = requests.post(endpoint + STATUS_PATH, json = payload, headers=headers)
            if (response.status_code == 201):
                return {'status': 'success', 'message': 'updated'}
            else:
                return {'status': response.status_code}
        except requests.exceptions.ConnectionError as e:
            logging.error("Check if the front-end server address known! or", e)
            pass
            # exit(1)
            # return {"message": "Error! Cannot connect to host machine"}


if __name__ == '__main__':
    logging.warning("Please make sure that backend server is reachable")
    loop_time = int(os.getenv("MANAGER_SLEEP_TIME", default=30))
    logging.info("BenchmarkManager will wait %s seconds between executions" % loop_time)

    manager = Manager()

    while(True):
        manager.start()
        time.sleep(loop_time)
