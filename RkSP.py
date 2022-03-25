#!/usr/bin/env python

from __future__ import division

import os
import sys
import subprocess
import signal
import socket
import logging
import thread
import time
import random
import numpy as np
import networkx as nx

from k_shortest_paths import k_shortest_paths
from optparse import OptionParser
from bs4 import BeautifulSoup

# We need to import Python modules from the $SUMO_HOME/tools directory
if 'SUMO_HOME' in os.environ:
    tools = os.path.join(os.environ['SUMO_HOME'], 'tools')
    sys.path.append(tools)
else:
    sys.exit("Environment variable SUMO_HOME not defined")

import traci


class UnusedPortLock:
    lock = thread.allocate_lock()

    def __init__(self):
        self.acquired = False

    def __enter__(self):
        self.acquire()

    def __exit__(self):
        self.release()

    def acquire(self):
        if not self.acquired:
            UnusedPortLock.lock.acquire()
            self.acquired = True

    def release(self):
        if self.acquired:
            UnusedPortLock.lock.release()
            self.acquired = False


def find_unused_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
    sock.bind(('127.0.0.1', 0))
    sock.listen(socket.SOMAXCONN)
    ipaddr, port = sock.getsockname()
    sock.close()

    return port


def terminate_sumo(sumo):
    if sumo.returncode == None:
        os.kill(sumo.pid, signal.SIGTERM)
        time.sleep(0.5)
        # if sumo.returncode == None:
        #     os.kill(sumo.pid, signal.SIGKILL)
        #     time.sleep(1)
        if sumo.returncode == None:
            time.sleep(10)


def build_road_graph(network):                
    # Input   
    f = open(network)
    data = f.read()
    soup = BeautifulSoup(data, "xml")
    f.close()
    
    edges_length = {}
    
    for edge_tag in soup.findAll("edge"):
        lane_tag = edge_tag.find("lane")
        
        edge_id = edge_tag["id"]
        edge_length = float(lane_tag["length"])
        edge_max_speed = float(lane_tag["speed"])
        
        edges_length[edge_id] = edge_length
    
    graph = nx.DiGraph()            
    
    for connection_tag in soup.findAll("connection"):
        source_edge = connection_tag["from"]        
        dest_edge = connection_tag["to"]
                
        graph.add_edge(source_edge.encode("ascii"), dest_edge.encode("ascii"),
            length=edges_length[source_edge], speed=edge_max_speed, weight=0)

    return graph

def update_road_attributes(graph, time, begin_of_cycle, delta):
    congestedRoads = set()

    for road in graph.nodes_iter():
        travel_time = traci.edge.getAdaptedTraveltime(road.encode("ascii"), time)
        if travel_time <= 0:
            travel_time = traci.edge.getTraveltime(road.encode("ascii"))

        if road.startswith(':'): 
            continue

        speedList = []
        for vehicle in traci.edge.getLastStepVehicleIDs(road.encode("ascii")):
            speedList.append(traci.vehicle.getSpeed(vehicle))

        if len(speedList) > 0:
            avgSpeed = np.asarray(speedList).mean()
        else:
            avgSpeed = 0

        for successor_road in graph.successors_iter(road):
            maxSpeed = graph.edge[road][successor_road]["speed"]
            if (avgSpeed/maxSpeed) > delta:
                congestedRoads.add(road.encode("ascii"))

            # If it is the first measurement in a cycle, then do not compute the mean
            if begin_of_cycle:
                graph.edge[road][successor_road]["weight"] = travel_time
            else:
                t = (graph.edge[road][successor_road]["weight"] + travel_time) / 2
                t = t if t > 0 else travel_time
                graph.edge[road][successor_road]["weight"] = t

    return (graph, congestedRoads)

def rksp_reroute(vehicles, graph, K):
    for vehicle in vehicles:
        source = traci.vehicle.getRoadID(vehicle)
        if source.startswith(":"):
            continue
        route = traci.vehicle.getRoute(vehicle)
        destination = route[-1]

        if source != destination:
            logging.debug("Calculating shortest paths for pair (%s, %s)" % (source, destination))
            _, k_paths = k_shortest_paths(graph, source, destination, K, "weight")

            random_path = random.randrange(0, len(k_paths))
            traci.vehicle.setRoute(vehicle, k_paths[random_path])


def select_vehicles(graph, congestedRoads, L):
    reverseGraph = nx.DiGraph.reverse(graph)
    selectedVehicles = set()
    
    for road in congestedRoads:
        count = 0
        bfs = []
        for edge in list(nx.bfs_edges(reverseGraph, road)):
            if edge[1].startswith(":"): continue

            if edge[0] in bfs:
                count += 1
                bfs = []
            
            if count == L:
                break

            if count == 0 and edge[0] not in bfs:
                selectedVehicles = selectedVehicles.union(set(traci.edge.getLastStepVehicleIDs(edge[0].encode('ascii'))))

            selectedVehicles = selectedVehicles.union(set(traci.edge.getLastStepVehicleIDs(edge[1].encode('ascii'))))

            bfs.append(edge[1])
    
    return selectedVehicles

def run(network, begin, end, interval, k, delta, level):
    logging.debug("Building road graph")
    road_graph = build_road_graph(network)
    logging.debug("Finding all simple paths")

    # Used to enhance performance only
    buffered_paths = {}

    logging.debug("Running simulation now")
    step = 1
    # The time at which the first re-routing will happen
    # The time at which a cycle for collecting travel time measurements begins
    travel_time_cycle_begin = interval

    while step == 1 or traci.simulation.getMinExpectedNumber() > 0:
        logging.debug("Minimum expected number of vehicles: %d" % traci.simulation.getMinExpectedNumber())
        traci.simulationStep()
        logging.debug("Simulation time %d" % step)

        if step >= travel_time_cycle_begin and travel_time_cycle_begin <= end and step % interval == 0:
            logging.debug("Updating travel time on roads at simulation time %d" % step)
            road_graph, congestedRoads = update_road_attributes(
                road_graph, step, travel_time_cycle_begin == step, delta)

            if len(congestedRoads) > 0:
                selectedVehicles = select_vehicles(road_graph, congestedRoads, level)
                logging.debug("Rerouting vehicles at simulation time %d" % step)
                rksp_reroute(selectedVehicles, road_graph, k)

        step += 1

    time.sleep(10)
    logging.debug("Simulation finished")
    traci.close()
    sys.stdout.flush()
    time.sleep(10)


def start_simulation(sumo, scenario, network, begin, end, interval, output, k, delta, level):
    logging.debug("Finding unused port")

    unused_port_lock = UnusedPortLock()
    unused_port_lock.__enter__()
    remote_port = find_unused_port()

    logging.debug("Port %d was found" % remote_port)

    logging.debug("Starting SUMO as a server")

    sumo = subprocess.Popen([sumo, "-c", scenario, "--tripinfo-output", output, "--device.emissions.probability",
                            "1.0", "--remote-port", str(remote_port)], stdout=sys.stdout, stderr=sys.stderr)
    unused_port_lock.release()

    try:
        traci.init(remote_port)
        run(network, begin, end, interval, k, delta, level)
    except Exception, e:
        logging.exception("Something bad happened")
    finally:
        logging.exception("Terminating SUMO")
        terminate_sumo(sumo)
        unused_port_lock.__exit__()


def main():
    # Option handling
    parser = OptionParser()
    parser.add_option("-c", "--command", dest="command", default="sumo",
                      help="The command used to run SUMO [default: %default]", metavar="COMMAND")
    parser.add_option("-s", "--scenario", dest="scenario", default="scenario/sim.sumocfg",
                      help="A SUMO configuration file [default: %default]", metavar="FILE")
    parser.add_option("-n", "--network", dest="network", default="scenario/sim.net.xml",
                      help="A SUMO network definition file [default: %default]", metavar="FILE")
    parser.add_option("-b", "--begin", dest="begin", type="int", default=1000, action="store",
                      help="The simulation time (s) at which the re-routing begins [default: %default]", metavar="BEGIN")
    parser.add_option("-e", "--end", dest="end", type="int", default=10000, action="store",
                      help="The simulation time (s) at which the re-routing ends [default: %default]", metavar="END")
    parser.add_option("-i", "--interval", dest="interval", type="int", default=900, action="store",
                      help="The interval (s) of classification [default: %default]", metavar="INTERVAL")
    parser.add_option("-o", "--output", dest="output", default="output/RkSP-HCM-tripinfo.xml",
                      help="The XML file at which the output must be written [default: %default]", metavar="FILE")
    parser.add_option("--logfile", dest="logfile", default="log/sumo-launchd.log",
                      help="log messages to logfile [default: %default]", metavar="FILE")
    parser.add_option("-k", "--k-paths", dest="k", type="int", default=3, action="store",
                      help="Number o k shortest paths [default: %default]", metavar="K")
    parser.add_option("-d", "--delta", dest="delta", type="float", default=0.7,
                      action="store", help="Congestion threshold [default: %default]", metavar="DELTA")
    parser.add_option("-l", "--level", dest="level", type="int", default=3, action="store",
                      help="Furthest distance a rerouted vehicle can be from congestion (in number of segments) [default: %default]", metavar="LEVEL")
    parser.add_option("--seed", dest="seed", type="int", default=42, action="store",
                      help="Seed used for random number generation [default: %default]", metavar="SEED")

    (options, args) = parser.parse_args()

    logging.basicConfig(filename=options.logfile, level=logging.DEBUG)
    logging.debug("Logging to %s" % options.logfile)

    if args:
        logging.warning("Superfluous command line arguments: \"%s\"" % " ".join(args))

    random.seed(options.seed)
    start_simulation(options.command, options.scenario, options.network, options.begin, options.end,
            options.interval, options.output, options.k, options.delta, options.level)

if __name__ == "__main__":
    main()
