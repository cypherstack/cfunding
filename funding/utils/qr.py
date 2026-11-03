import asyncio
import os
from io import BytesIO

import pyqrcode
from PIL import Image, ImageDraw

from funding.utils import validate_crypto_address
from funding.utils.cache import cache_file_path, write_cache_file
import settings

QR_LOCK = asyncio.Lock()


class QrCodeGenerator:
    def __init__(self):
        self.base = os.path.join(settings.cwd, 'funding', 'static', 'qr')
        self.image_size = (300, 300)
        self.pil_save_options = {
            'quality': 25,
            'optimize': True
        }

        if not os.path.exists(self.base):
            os.mkdir(self.base)

    def cache_path(self, address):
        if not isinstance(address, str) or not address.isascii() or \
                not address.isalnum() or not validate_crypto_address(address):
            raise ValueError('Invalid QR address')
        return cache_file_path(self.base, f'{address}.png')

    def exists(self, address):
        return os.path.isfile(self.cache_path(address))

    def create(self, address, dest=None,
               color_from=(170,43,43),
               color_to=(230,51,51)):
        """
        Create QR code image. Optionally a gradient.
        :param address:
        :param dest:
        :param color_from: gradient from color
        :param color_to:  gradient to color
        :return:
        """
        destination = self.cache_path(address)
        if dest is not None:
            candidate = cache_file_path(self.base, os.path.basename(dest))
            if os.path.realpath(dest) != candidate:
                raise ValueError('QR destination must remain inside its cache directory')
            destination = candidate

        created = pyqrcode.create(address, error='L')
        buffer = BytesIO()
        created.png(buffer, scale=14, quiet_zone=2)

        im = Image.open(buffer)
        im = im.convert("RGBA")
        im.thumbnail(self.image_size)

        im_data = im.getdata()

        # make black color transparent
        im_transparent = []
        for color_point in im_data:
            if sum(color_point[:3]) == 255 * 3:
                im_transparent.append(color_point)
            else:
                # get rid of the subtle grey borders
                alpha = 0 if color_from and color_to else 1
                im_transparent.append((0, 0, 0, alpha))
                continue

        if not color_from and not color_to:
            output = BytesIO()
            im.save(output, format='PNG', **self.pil_save_options)
            return write_cache_file(self.base, os.path.basename(destination), output.getvalue())

        # turn QR into a gradient
        im.putdata(im_transparent)

        gradient = Image.new('RGBA', im.size, color=0)
        draw = ImageDraw.Draw(gradient)

        for i, color in enumerate(QrCodeGenerator.gradient_interpolate(color_from, color_to, im.width * 2)):
            draw.line([(i, 0), (0, i)], tuple(color), width=1)

        im_gradient = Image.alpha_composite(gradient, im)
        output = BytesIO()
        im_gradient.save(output, format='PNG', **self.pil_save_options)
        return write_cache_file(self.base, os.path.basename(destination), output.getvalue())

    @staticmethod
    def gradient_interpolate(color_from, color_to, interval):
        det_co = [(t - f) / interval for f, t in zip(color_from, color_to)]
        for i in range(interval):
            yield [round(f + det * i) for f, det in zip(color_from, det_co)]
