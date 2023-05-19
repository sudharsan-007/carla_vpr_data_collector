#!/usr/bin/env python

# Copyright (c) 2023 AI4CE Lab under New York University
#
# This work is licensed under the terms of the MIT license.
# For a copy, see <https://opensource.org/licenses/MIT>.


"""
Welcome to CARLA manual control VPR data collector.

Use ARROWS or WASD keys for control.

    W            : throttle
    S            : brake
    A/D          : steer left/right
    Q            : toggle reverse
    Space        : hand-brake
    P            : toggle autopilot
    CTRL + W     : toggle constant velocity mode at 60 km/h

    L            : toggle next light type
    SHIFT + L    : toggle high beam

    TAB          : change sensor position
    ` or N       : next sensor
    [1-8]        : change to sensor [1-8] 
    C            : change weather (Shift+C reverse)
    Backspace    : change vehicle

    R            : toggle recording images to disk
    CTRL + R     : toggle recording of simulation (replacing any previous)
    H/?          : toggle help
    ESC          : quit
"""

from __future__ import print_function


# ==============================================================================
# -- find carla module ---------------------------------------------------------
# ==============================================================================


import glob
import os
import sys

try:
    sys.path.append(glob.glob('../carla/dist/carla-*%d.%d-%s.egg' % (
        sys.version_info.major,
        sys.version_info.minor,
        'win-amd64' if os.name == 'nt' else 'linux-x86_64'))[0])
except IndexError:
    pass


# ==============================================================================
# -- imports -------------------------------------------------------------------
# ==============================================================================

import cv2
import carla
import pygame

from carla import ColorConverter as cc

import argparse 
import logging
import pandas as pd


try:
    import numpy as np
except ImportError:
    raise RuntimeError('cannot import numpy, make sure numpy package is installed')


from utils import find_weather_presets, get_actor_display_name, HUD, FadingText
from keyboardcontrol import KeyboardControl
from camera import CameraManager
from sensors import GnssSensor


global loc, recording

recording = False
loc = None


# =============================================================================
# -- Data Recording -----------------------------------------------------------
# =============================================================================

class DataRecorder():
    def __init__(self, data_dir, cam_res_x, cam_res_y):
        self.data_dir = data_dir
        self.cam_res_width, self.cam_res_height = cam_res_x, cam_res_y
        if not os.path.isdir(data_dir):
            os.makedirs(data_dir)
        if not os.path.isfile(os.path.join(self.data_dir,"data.csv")):
            pd.DataFrame(None, columns=["Frame","x","y", "yaw"],).to_csv(f"{self.data_dir}/data.csv")
            print("No previous data found, creating new file")
        if not os.path.isdir(str(data_dir)+"/cam1"):
            os.makedirs(str(data_dir)+"/cam1")
        if not os.path.isdir(str(data_dir)+"/cam2"):
            os.makedirs(str(data_dir)+"/cam2")
        if not os.path.isdir(str(data_dir)+"/cam3"):
            os.makedirs(str(data_dir)+"/cam3")
    
    def data_processing(self, image, sub_dir, recording):
        i = np.asarray(image.raw_data)
        depth_rgb = i.reshape((self.cam_res_height, self.cam_res_width, 4))
        img = depth_rgb[:, :, :3]
        # print(loc)
        #w, h, c = i3.shape
        if recording:
            frame_name = "f{:08d}".format(image.frame)
            
            cv2.imwrite(str(self.data_dir)+"/"+str(sub_dir)+"/"+frame_name+".jpg", img)
    
            data = {
                    'Frame': [str(frame_name)],
                    'x': [str(loc.location.x)],
                    'y': [str(loc.location.y)],
                    'yaw': [str(loc.rotation.yaw)]
                }
            df = pd.DataFrame(data)
            df.to_csv(f"{self.data_dir}/data.csv", mode='a', index=False, header=False)
        
    def img_processing(self, image, sub_dir, recording):
        i = np.asarray(image.raw_data)
        depth_rgb = i.reshape((self.cam_res_height, self.cam_res_width, 4))
        img = depth_rgb[:, :, :3]
        # print(loc)
        #w, h, c = i3.shape
        if recording:
            frame_name = "f{:08d}".format(image.frame)
            
            cv2.imwrite(str(self.data_dir)+"/"+str(sub_dir)+"/"+frame_name+".jpg", img)
            

# ==============================================================================
# -- World ---------------------------------------------------------------------
# ==============================================================================


class World(object):
    def __init__(self, carla_world,hud, args):
        self.world = carla_world
        self.sync = args.sync
        self.actor_role_name = args.rolename
        self.cam_res_x, self.cam_res_y = args.cam_res_x, args.cam_res_y
        try:
            self.map = self.world.get_map()
        except RuntimeError as error:
            print('RuntimeError: {}'.format(error))
            print('  The server could not send the OpenDRIVE (.xodr) file:')
            print('  Make sure it exists, has the same name of your town, and is correct.')
            sys.exit(1)
        self.hud = hud
        self.player = None
        self.recording = False
        self.gnss_sensor = None
        self.camera_manager = None
        self._weather_presets = find_weather_presets()
        self._weather_index = 0
        self._gamma = args.gamma
        self.data_recorder = DataRecorder("data", self.cam_res_x, self.cam_res_y)
        self.restart()
        self.world.on_tick(hud.on_world_tick)
        print("spawned")
        self.constant_velocity_enabled = False


    def restart(self):
        self.player_max_speed = 1.589
        self.player_max_speed_fast = 3.713
        # Keep same camera config if the camera manager exists.
        cam_index = self.camera_manager.index if self.camera_manager is not None else 0
        cam_pos_index = self.camera_manager.transform_index if self.camera_manager is not None else 0
        
        # Get a player blueprint.    
        bp_lib = self.world.get_blueprint_library()
        blueprint = bp_lib.find('vehicle.lincoln.mkz_2020')
        
        blueprint.set_attribute('role_name', self.actor_role_name)
        if blueprint.has_attribute('terramechanics'):
            blueprint.set_attribute('terramechanics', 'true')
        if blueprint.has_attribute('color'):
            color = blueprint.get_attribute('color').recommended_values[0]
            blueprint.set_attribute('color', color)
        if blueprint.has_attribute('driver_id'):
            driver_id = blueprint.get_attribute('driver_id').recommended_values[0]
            # print(blueprint.get_attribute('driver_id').recommended_values)
            blueprint.set_attribute('driver_id', driver_id)
        if blueprint.has_attribute('is_invincible'):
            blueprint.set_attribute('is_invincible', 'true')
        # set the max speed
        if blueprint.has_attribute('speed'):
            self.player_max_speed = float(blueprint.get_attribute('speed').recommended_values[1])
            self.player_max_speed_fast = float(blueprint.get_attribute('speed').recommended_values[2])

        # Spawn the player.
        if self.player is not None:
            spawn_point = self.player.get_transform()
            spawn_point.location.z += 2.0
            spawn_point.rotation.roll = 0.0
            spawn_point.rotation.pitch = 0.0
            self.destroy()
            self.player = self.world.try_spawn_actor(blueprint, spawn_point)
            self.modify_vehicle_physics(self.player)
        while self.player is None:
            if not self.map.get_spawn_points():
                print('There are no spawn points available in your map/town.')
                print('Please add some Vehicle Spawn Point to your UE4 scene.')
                sys.exit(1)
            spawn_points = self.map.get_spawn_points()
            spawn_point = spawn_points[10] if spawn_points else carla.Transform()
            self.player = self.world.try_spawn_actor(blueprint, spawn_point)
            self.modify_vehicle_physics(self.player)
        # Set up the sensors.
        self.gnss_sensor = GnssSensor(self.player)
        self.camera_manager = CameraManager(self.player, self.hud, self._gamma)
        self.camera_manager.transform_index = cam_pos_index
        self.camera_manager.set_sensor(cam_index, notify=False)
        
        camera_bp = bp_lib.find('sensor.camera.rgb')

        camera_bp.set_attribute('image_size_x', str(self.cam_res_x))
        camera_bp.set_attribute('image_size_y', str(self.cam_res_y)) 
        camera_bp.set_attribute('fov', '120') 

        camera_init_trans1 = carla.Transform(carla.Location(x=0.5,z=3.4), carla.Rotation(yaw=0))  
        camera_init_trans2 = carla.Transform(carla.Location(x=0.5,z=3.4), carla.Rotation(yaw=120)) 
        camera_init_trans3 = carla.Transform(carla.Location(x=0.5,z=3.4), carla.Rotation(yaw=240)) 
        
        self.camera1 = self.world.spawn_actor(camera_bp, camera_init_trans1, attach_to=self.player) 
        # self.camera2 = self.world.spawn_actor(camera_bp, camera_init_trans2, attach_to=self.player) 
        # self.camera3 = self.world.spawn_actor(camera_bp, camera_init_trans3, attach_to=self.player) 

        self.camera1.listen(lambda image1: self.data_recorder.data_processing(image1, "cam1", self.recording)) 
        # self.camera2.listen(lambda image2: self.data_recorder.img_processing(image2, "cam2", self.recording)) 
        # self.camera3.listen(lambda image3: self.data_recorder.img_processing(image3, "cam3", self.recording)) 

        if self.sync:
            self.world.tick()
        else:
            self.world.wait_for_tick()

    def next_weather(self, reverse=False):
        self._weather_index += -1 if reverse else 1
        self._weather_index %= len(self._weather_presets)
        preset = self._weather_presets[self._weather_index]
        print('Weather: %s' % preset[1])
        self.player.get_world().set_weather(preset[0])

    def modify_vehicle_physics(self, actor):
        #If actor is not a vehicle, we cannot use the physics control
        try:
            physics_control = actor.get_physics_control()
            physics_control.use_sweep_wheel_collision = True
            actor.apply_physics_control(physics_control)
        except Exception:
            pass

    def tick(self, clock):
        self.hud.tick(self, clock)
        #print('lat:{}, lon{}'.format(self.gnss_sensor.lat, self.gnss_sensor.lon)) 
        global loc
        loc = self.player.get_transform()
        # print(loc.location.x, loc.location.y, loc.rotation.yaw) 

    def render(self, display):
        self.camera_manager.render(display)
        self.hud.render(display)

    def destroy_sensors(self):
        self.camera_manager.sensor.destroy()
        self.camera_manager.sensor = None
        self.camera_manager.index = None

    def destroy(self):
        sensors = [
            self.camera_manager.sensor,
            self.gnss_sensor.sensor,
            self.camera1,
            self.camera2,
            self.camera3
            ]
        for sensor in sensors:
            if sensor is not None:
                sensor.stop()
                sensor.destroy()
        if self.player is not None:
            self.player.destroy()



# ==============================================================================
# -- game_loop() ---------------------------------------------------------------
# ==============================================================================


def game_loop(args):
    pygame.init()
    pygame.font.init()
    world = None
    original_settings = None

    try:
        client = carla.Client(args.host, args.port)
        client.set_timeout(2000.0)

        sim_world = client.get_world()
        if args.sync:
            original_settings = sim_world.get_settings()
            settings = sim_world.get_settings()
            if not settings.synchronous_mode:
                settings.synchronous_mode = True
                settings.fixed_delta_seconds = 0.05
            sim_world.apply_settings(settings)

            traffic_manager = client.get_trafficmanager()
            traffic_manager.set_synchronous_mode(True)

        if args.autopilot and not sim_world.get_settings().synchronous_mode:
            print("WARNING: You are currently in asynchronous mode and could "
                  "experience some issues with the traffic simulation")

        display = pygame.display.set_mode(
            (args.width, args.height),
            pygame.HWSURFACE | pygame.DOUBLEBUF)
        display.fill((0,0,0))
        pygame.display.flip()

        hud = HUD(args.width, args.height)
        world = World(sim_world, hud, args)
        controller = KeyboardControl(world, args.autopilot)

        if args.sync:
            sim_world.tick()
        else:
            sim_world.wait_for_tick()

        clock = pygame.time.Clock()
        while True:
            if args.sync:
                sim_world.tick()
            clock.tick_busy_loop(30)
            if controller.parse_events(client, world, clock, args.sync):
                return
            world.tick(clock)
            world.render(display)
            pygame.display.flip()

    finally:

        if original_settings:
            sim_world.apply_settings(original_settings)

        if world is not None:
            world.destroy()

        pygame.quit()


# ==============================================================================
# -- main() --------------------------------------------------------------------
# ==============================================================================


def main():
    argparser = argparse.ArgumentParser(
        description='CARLA Manual Control Client')
    argparser.add_argument(
        '-v', '--verbose',
        action='store_true',
        dest='debug',
        help='print debug information')
    argparser.add_argument(
        '--host',
        metavar='H',
        default='127.0.0.1',
        help='IP of the host server (default: 127.0.0.1)')
    argparser.add_argument(
        '-p', '--port',
        metavar='P',
        default=2000,
        type=int,
        help='TCP port to listen to (default: 2000)')
    argparser.add_argument(
        '-a', '--autopilot',
        action='store_true',
        help='enable autopilot')
    argparser.add_argument(
        '--res',
        metavar='WIDTHxHEIGHT',
        default='1280x720',
        help='window resolution (default: 1280x720)')
    argparser.add_argument(
        '--camres',
        metavar='CAMWIDTHxCAMHEIGHT',
        default='640x480',
        help='cam resolution (default: 640x480)')
    argparser.add_argument(
        '--rolename',
        metavar='NAME',
        default='hero',
        help='actor role name (default: "hero")')
    argparser.add_argument(
        '--gamma',
        default=2.2,
        type=float,
        help='Gamma correction of the camera (default: 2.2)')
    argparser.add_argument(
        '--sync',
        action='store_true',
        help='Activate synchronous mode execution')
    args = argparser.parse_args()

    args.width, args.height = [int(x) for x in args.res.split('x')]
    args.cam_res_x, args.cam_res_y = [int(x) for x in args.camres.split('x')]

    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(format='%(levelname)s: %(message)s', level=log_level)

    logging.info('listening to server %s:%s', args.host, args.port)

    print(__doc__)

    try:

        game_loop(args)

    except KeyboardInterrupt:
        print('\nCancelled by user. Bye!')


if __name__ == '__main__':

    main()