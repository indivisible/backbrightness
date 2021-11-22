#!/usr/bin/env python3

# based on:
# https://gist.github.com/joel-wright/68fc3031cbb3f7cd25db1ed2fe656e60

import os
import time
from abc import ABC, abstractmethod
from pathlib import Path
from functools import lru_cache
from typing import Optional


class BrightnessSetter(ABC):
    def __init__(self, output_names: list[str]):
        self.crtcs = {}
        self.crtcs = {}
        for name in output_names:
            crtc = {
                'id': None,
                'original_gamma': None,
            }
            self.crtcs[name] = crtc

    @lru_cache(maxsize=10)
    def generate_gamma_table(self, crtc_name: str, brightness: float = 1.0):
        table = self.crtcs[crtc_name]['original_gamma']
        if brightness is None:
            return table
        return tuple(
            list(int(i * brightness) for i in channel) for channel in table)

    @abstractmethod
    def set_brightness(self, brightness: Optional[float]):
        pass

    def reset(self):
        self.set_brightness(None)


# this has the happy consequence of resetting gamma
class XRandRBrightnessSetter(BrightnessSetter):
    def __init__(self,
                 output_names: list[str] = [],
                 display_name: Optional[str] = None,
                 screen_num: Optional[int] = None):
        super().__init__(output_names)
        self.connection = None
        self.display_name = display_name
        self.screen_num = screen_num

    def __find_crtcs(self):
        for crtc in self.crtcs.values():
            crtc['id'] = None

        randr = self.randr
        crtc_ids = randr.GetScreenResources(self.screen.root).reply().crtcs
        for crtc_id in crtc_ids:
            crtc_info = randr.GetCrtcInfo(crtc_id, int(time.time())).reply()
            for output in crtc_info.outputs:
                output_info = randr.GetOutputInfo(output,
                                                  int(time.time())).reply()
                name = bytes(output_info.name).decode('ascii')
                print('met crtc {}'.format(name))
                crtc = self.crtcs.get(name)
                if crtc is not None:
                    crtc['id'] = crtc_id
                    if crtc['original_gamma'] is None:
                        reply = randr.GetCrtcGamma(crtc_id).reply()
                        crtc['original_gamma'] = [
                            reply.red, reply.green, reply.blue
                        ]

    def connect(self):
        import xcffib
        import xcffib.randr

        def default_if_none(value, default):
            if value is None:
                return default
            return value

        if not self.connection:
            disp_name = default_if_none(self.display_name,
                                        os.environ.get('DISPLAY'))
            self.connection = con = xcffib.connect(disp_name)
            screen_num = default_if_none(self.screen_num, con.pref_screen)
            self.screen = con.get_setup().roots[screen_num]
            self.randr = con(xcffib.randr.key)
            self.__find_crtcs()

        return self.randr

    def set_brightness(self, brightness: Optional[float]):
        randr = self.connect()
        assert self.connection
        for name, crtc in self.crtcs.items():
            if crtc['id'] is None:
                continue

            adjusted = self.generate_gamma_table(name, brightness)
            randr.SetCrtcGamma(crtc['id'], len(adjusted[0]), adjusted[0],
                               adjusted[1], adjusted[2])
        self.connection.flush()


# this is pretty damn resource intensive
class GnomeBrightnessSetter(BrightnessSetter):
    def __init__(self, output_names: list[str] = []):
        super().__init__(output_names)
        from pydbus import SessionBus

        self.bus = SessionBus()
        self.serial = None
        self.display_conf = self.bus.get('org.gnome.Mutter.DisplayConfig')

        self.init_configuration()

    def init_configuration(self):
        res = self.display_conf.GetResources()

        if res[0] == self.serial:
            return

        self.serial = res[0]
        for crtc in self.crtcs.values():
            crtc['id'] = None

        for output in res[2]:
            crtc_id = output[2]
            name = output[4]

            if name in self.crtcs:
                crtc = self.crtcs[name]
                crtc['id'] = crtc_id
                if crtc['original_gamma'] is None:
                    gamma = self.display_conf.GetCrtcGamma(
                        self.serial, crtc_id)
                    crtc['original_gamma'] = gamma

    def set_brightness(self, brightness: Optional[float]):
        self.init_configuration()
        for name, crtc in self.crtcs.items():
            if crtc['id'] is None:
                continue

            gamma = self.generate_gamma_table(name, brightness)
            self.display_conf.SetCrtcGamma(self.serial, crtc['id'], gamma[0],
                                           gamma[1], gamma[2])


def translate_backlight(setter, backlight_path: Path, sleep_time: float):
    max_path = backlight_path / 'max_brightness'
    actual_path = backlight_path / 'actual_brightness'

    try:
        # the reason we continually reset the gamma is that sometimes
        # applications reset the gamma (e.g. 1st time chromium starts)
        while True:
            with open(str(max_path), 'rt') as max_file, \
                    open(str(actual_path), 'rt') as actual_file:
                max_brightness = int(max_file.read())
                actual_brightness = int(actual_file.read())
            brightness = actual_brightness / max_brightness
            setter.set_brightness(brightness)

            time.sleep(sleep_time)
    finally:
        setter.reset()


def main():
    import argparse

    default_method = 'xrandr'
    methods = {
        'xrandr': XRandRBrightnessSetter,
        'gnome': GnomeBrightnessSetter,
    }

    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '-s',
        '--sleep-time',
        type=float,
        default=1.0,
        help='Time between two brightness updates. Lower is more '
        'responsive, but higher CPU usage.')
    parser.add_argument('backlight_path',
                        type=Path,
                        help='Path for the intel acpi backlight. For example '
                        '"/sys/class/backlight/intel_backlight"')
    parser.add_argument(
        'outputs',
        type=str,
        nargs='+',
        help='outputs whose brightness to adjust with randr. Check '
        '"xrandr -q" for values. Normally it should be eDP1 or eDP-1')
    parser.add_argument('--setter-method',
                        default=default_method,
                        choices=methods.keys(),
                        help='Which setter method to use.')

    args = parser.parse_args()

    setter = methods[args.setter_method](args.outputs)
    translate_backlight(setter, args.backlight_path, args.sleep_time)

    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
