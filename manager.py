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

import pymysql
pymysql.install_as_MySQLdb()

#client logs
#mysql
#frontend + secret route: scheduler REST submit result that manager accesses ("get nextjob")//("post result.json -> frontend upd DB")
#logs via securecopy ()
#benchmark

SPLIT_PART = 1 # index of string part of dockerhub image_name.split("/")
HOST = "http://127.0.0.1:8080"
SCHEDULE_PATH = os.getenv("SCHEDULE_PATH", default= '/schedule')
RESULT_PATH = os.getenv("RESULT_PATH", default='/result')

endpoint = os.getenv("FRONTEND_SERVER")
if endpoint is None:
    #logger.error("please specify front-end server address!")
    #exit(1)
    endpoint = HOST
if "docker" in endpoint:
    endpoint = 'http://' + endpoint + ":8080"

LOG_FOLDER_NAME = "manager_logs"
if not os.path.exists(LOG_FOLDER_NAME):
    os.makedirs(LOG_FOLDER_NAME)
filename = 'compose_manager.log'
logging.basicConfig(
                    level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(threadName)s -  %(levelname)s - %(message)s',
                    handlers=[
                     logging.FileHandler("{0}/{1}".format(LOG_FOLDER_NAME, filename)),
                     logging.StreamHandler()
                    ])
logger = logging.getLogger()

logging.info("API endpoint %s" % endpoint)

class Manager:

    def __init__(self):

        #self.client = docker.from_env()
        json.JSONEncoder.default = lambda self,obj: (obj.isoformat() if isinstance(obj, datetime.datetime) else None)
        self.images = []

    def find_container_ip_addr(self, container_name):
        info = subprocess.check_output(['docker', 'inspect', container_name])
        # parsing nested json from docker inspect
        ip = list(json.loads(info.decode('utf-8'))[0]["NetworkSettings"]["Networks"].values())[0]["IPAddress"]
        print("%s container ip is: %s" % (container_name, ip))
        return ip

    def execute(self, cmd):
        # prints output into console in real-time
        popen = subprocess.Popen(cmd, stdout=subprocess.PIPE, universal_newlines=True)
        for stdout_line in iter(popen.stdout.readline, ""):
            yield stdout_line
        popen.stdout.close()
        return_code = popen.wait()
        if return_code:
            #raise subprocess.CalledProcessError(return_code, cmd)
            logging.info("Docker-compose done executing")


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

        try:
            log_volume = log_volume[0]+ "/" + str(image.split("/")[1]) +":"+log_volume[1]
        except IndexError:
            log_volume = log_volume[0]+ "/" + str(image.split("/")[0]) +":"+log_volume[1]
        new_volumes = [volumes[0],log_volume]
        list_doc["services"]["server"]["volumes"] = new_volumes

        new_name = "docker-compose.yml" #str(split_name[0]) + str(file_number) + "."+ str(split_name[1])

        with open(new_name, "w") as f:
            yaml.dump(list_doc, f, default_flow_style=False)
        logging.info("docker compose file saved with name %s" % new_name)

    def get_schedule(self):
        scheduled_teams = self.table.distinct('name', updated='True')
        self.scheduled_teams = [ str(i['name']) for i in list(scheduled_teams) ]
        return self.scheduled_teams

    def uncheck_team(self, image):
        #self.images.remove(image)

        self.table.update(dict(team_image_name=image, updated='False'), ['team_image_name'])
        print("This image was unchecked: ", image)

    def compare_and_set(self, tag, image):
        try:
            old_tag = self.table.find(tag=tag)
            if old_tag['tag'] == tag:
                return False
            else:
                self.table.update(dict(name=old_tag['name'], tag=tag), ['name'])
                return True
        except Exception as e:
            #print(e)
            self.table.update(dict(team_image_name=image, tag=tag), ['team_image_name'])
            print("tag updated")


    def get_images(self):
        global endpoint
        updated_images = []
        data = requests.get(endpoint + SCHEDULE_PATH)
        try:
            images = data.json()

        except json.decoder.JSONDecodeError as e:
                logging.error(" Check if the front-end server is reachable! Cannot retrieve JSON response. %s" % e)
                exit(1)
        for image, status in images.items():
            if status == 'updated':
                try:
                    docker_hub_link = image.split('/')
                    updated_images.append(image)
                except IndexError:
                    print('Incorrectly specified image encountered. Format is {team_repo/team_image}')
                    continue
        return updated_images


    def grab_teams(self, teams_file):
        try:
            with open(teams_file.split('.')[0]+'.json') as f:
                data = json.load(f)
            return data

        except Exception as e:
            print(e)
            data = {}
            with open(teams_file) as f:
                for line in f:
                    line = re.sub(r"[,;\"\'\n\t\s]*", "",line)
                    data[line] = ""
            return data

    def save_container_log(self, cmd, logfile, extension):
        dir = "../logs/" + logfile.split('/')[SPLIT_PART]
        if not os.path.exists(dir):
            os.makedirs(dir)
        with open(dir + "/" + logfile.split('/')[SPLIT_PART] + extension, "w+") as f:
            p = subprocess.Popen(cmd, shell=True, universal_newlines=True, stdout=f)
            p.wait()
            #return p

    def process_result(self, docker_img_name, image_tag):
            #test
            global loop_time
            logging.info("Running image: %s " % docker_img_name)
            logging.info("Extracting results")
            team_result = self.extract_results(docker_img_name)
            #add image sha256
            if team_result:
                team_result['tag'] = image_tag
                team_result['last_run'] = datetime.datetime.utcnow().replace(microsecond=0).replace(second=0)
                team_result['piggybacked_manager_timeout'] = loop_time
                logging.info("Sending results: %s" % team_result)
                self.post_result(team_result)
                logging.info("Completed run for %s" % docker_img_name)
            else:
                logging.error("No results after becnhmark run of %s" % docker_img_name)
            sys.stdout.flush()

    def start(self):
        logging.info("----------------------------")
        logging.info("Benchmark Manager started...")
        benchmark_container_name = "benchmark-server-logging"
        client_container_name = "client-app-"

        #print(subprocess.check_output(['docker', 'version']))
        #self.get_schedule()
        self.images = self.get_images()
        #print("Got teams with images; ", self.scheduled_teams)
        # teams_file ='teams.txt'
        # repo_list = self.grab_teams(teams_file)
        # repo_list = OrderedDict(sorted(repo_list.items()))
        # print("Teams: ", repo_list)
        # print("Tags: ", repo_list.values())



        try:
            subprocess.check_output(['docker', 'stop', benchmark_container_name])
            subprocess.check_output(['docker', 'rm', benchmark_container_name])

        except subprocess.CalledProcessError as e:
            logging.info("Trying cleanup. Got: %s. Proceeding!" % e)
            pass

        logging.info("Current scheduled images: %s" % self.images)
        for docker_img_name in self.images:

            try:
                #subprocess.check_output(['docker', 'rmi', docker_img_name])
                subprocess.check_output(['docker', 'rm', client_container_name+docker_img_name.split("/")[SPLIT_PART]])
            except Exception as e:
                logging.info("Trying cleanup. Got: %s. Proceeding!" %e)
                pass

            tag = ""
            try:
                logging.info("Pulling image ........... %s" % docker_img_name)
                #self.client.images.pull(docker_img_name)
                subprocess.check_output(['docker', 'pull', docker_img_name])
                tag = subprocess.check_output(['docker', 'inspect', docker_img_name])
                tag = json.loads(tag.decode('utf-8'))[0]["Id"]
                logging.info("GOT tag: %s" % tag)
                # if repo_list[docker_img_name] != tag:
                #     repo_list[docker_img_name] = tag
                #     print(repo_list)
                #     with open(teams_file.split('.')[0]+'.json', 'w') as outfile:
                #         json.dump(repo_list, outfile)
                #     logging.info("pulled image: %s" % docker_img_name)
            except Exception as e:
                #logging.error("Error during pull happened %s" % e)
                logging.error("Probably can't access image: %s. Error %s" % (docker_img_name, e))
                continue

            # try:
            #     img = self.client.images.get(docker_img_name)
            # except docker.errors.ImageNotFound as e:
            #     #logging.error("Error during pull happened %s" % e)
            #     logging.error(e)
            #     pass

            # img.tag(new_img_name, tag= 'latest')
            # logging.info("tagged image: %s and tagged to: %s" % (docker_img_name, new_img_name))
            # subprocess.check_output(['docker' ,'rmi', docker_img_name])
            # logging.info("untagged old image %s" % docker_img_name)
            #new_container_name = benchmark_container_name + docker_img_name.split("/")[1]
            # print("Current dir is %s " % os.getcwd())
            container_name = client_container_name+docker_img_name.split("/")[SPLIT_PART]
            self.create_docker_compose_file(docker_img_name, container_name) #TODO change for [0] for client repo name
            #logging.info("calling docker-compose -f  up ")

            # subprocess.check_output(['docker-compose' ,'up', '-d', '--build'])
            #subprocess.check_output(['docker-compose', 'up', '--build', '--abort-on-container-exit'])
            # = subprocess.Popen(,stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            cmd = ['docker-compose', 'up', '--build', '--abort-on-container-exit']
            # real-time output
            for path in self.execute(cmd):
                # print(path, "")
                logging.info(path)
                sys.stdout.flush()

            logging.info("docker-compose exited")

            client_container = client_container_name+docker_img_name.split("/")[SPLIT_PART]
            # print(subprocess.check_output(['docker', 'logs', container]))
            cmd1 = 'docker logs ' + client_container
            filename =  docker_img_name
            self.save_container_log(cmd1, filename, '_client.txt')
            cmd2 = 'docker logs ' + benchmark_container_name
            self.save_container_log(cmd2, filename, '_bench.txt')
            logging.info("Container logs saved")

            self.process_result(docker_img_name, tag)




        #print("tagged image: %s and tagged to: %s" % (docker_img_name, new_docker_img_name))
        #logging.info(client.containers.get('kafkamessagetier_zoo1_1').logs().split("\n"))
        # logs
        # for line in client.containers.get(benchmark_container_name).logs().split("\n"):
        #     if "WARN" in line:
        #         logging.warning(line)
        #     else:
        #         logging.info(line)
        #
        # subprocess.check_output(['docker-compose' ,'stop'])

        #
        # logging.info("removing image %s" % new_docker_img_name)
        # subprocess.check_output(['docker' ,'rmi', new_docker_img_name])
        # logging.info("image %s was removed" % new_docker_img_name )
        # logging.info("removing container %s" % benchmark_container_name)
        # subprocess.check_output(['docker' ,'rm', benchmark_container_name])
        logging.info("Evaluation is completed")
        self.images = []
        #subprocess.check_output(['docker', 'rmi repox:edge-edge'])
        #subprocess.run(['docker', 'images'])
        return

    def extract_results(self, full_image_name):
        logging.info("Looking for log folders")
        rootdir = "./logs"
        #pwd = os.getcwd()
        #print("pwd: ", pwd)
        if "logs" in os.walk(rootdir):
            pass
        else:
            rootdir = "../logs"
        overall_data = {}

        dir = full_image_name.split('/')[SPLIT_PART]
        list_of_files = os.listdir(rootdir+"/"+dir)
        #print("files", list_of_files)
        list_of_files = [i for i in list_of_files if ".json" in i]
        if not list_of_files:
            logging.warning('No file result.json yet')
            return {}
        fresh_log = list_of_files[0]
        #print(fresh_log)
        runtime = 0
        accuracy = 0
        recall = 0
        precision = 0
        computed_scenes = 0

        with open(rootdir + "/"+ dir+ "/"+fresh_log) as f:
            data = json.load(f)
            data['team_image_name'] = full_image_name
            logging.info("data in %s is: %s" % (dir,data))
        return data

    def post_result(self, payload):
        global endpoint
        headers = {'Content-type': 'application/json'}
        try:
            response = requests.post(endpoint + RESULT_PATH, json = payload, headers=headers)

            #print('Response status is: ', response.status_code)
            if (response.status_code == 201):
                return {'status': 'success', 'message': 'updated'}
            if (response.status_code == 404):
                return {'message': 'Something went wrong. No scene exist. Check if the path is correct'}
        except requests.exceptions.ConnectionError as e:
            logging.error("Check if the front-end server address known! or", e)
            exit(1)
            #return {"message": "Error! Cannot connect to host machine"}

if __name__ == '__main__':
    #time.sleep(30)
    logging.warning("Please make sure that backend server is reachable")
    manager = Manager()


    # manager.get_schedule()
    # manager.get_images()
    # print("Got images; ", manager.images)
    # print("Got teams; ", manager.scheduled_teams)
    loop_time = int(os.getenv("MANAGER_SLEEP_TIME", default=30))
    logging.info("BenchmarkManager will wait %s sec between executions" % loop_time)
    while(True):
        #manager.connect_to_db()
        manager.start()
        time.sleep(loop_time)
