from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
import io
import os
import re
import sys
import cv2
import math
import json
import time
import random
import requests
import shapefile
import geopandas
import shutil
import numpy as np
import urllib.request as ur

from osgeo import gdal, ogr
from osgeo.gdalconst import *

from shapely import geometry
from threading import Thread, Lock
from matplotlib import pyplot as plt

from PIL import Image
from PIL import ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = None


# 旧 "amap": "http://wprd02.is.autonavi.com/appmaptile?style={style}&x={x}&y={y}&z={z}",
MAP_URLS = {
    "google1": "http://mt{server}.google.cn/vt/lyrs={style}&hl=zh-CN{offset}&src=app&x={x}&y={y}&z={z}",
    "google2": "https://gac-geo.googlecnapps.cn/maps/vt/lyrs={style}&hl=zh-CN{offset}&src=app&x={x}&y={y}&z={z}",
    "google3": "http://www.google.cn/maps/vt/lyrs={style}&hl=zh-CN{offset}&src=app&x={x}&y={y}&z={z}",
    "amap":"http://wprd02.is.autonavi.com/appmaptile?x={x}&y={y}&z={z}&lang=zh_cn&size=1&scl=2&style={style}",
    "tencent_s": "http://p3.map.gtimg.com/sateTiles/{z}/{fx}/{fy}/{x}_{y}.jpg",
    "tencent_m": "http://rt0.map.gtimg.com/tile?z={z}&x={x}&y={y}&styleid=3"
}

HIT_COUNT = 0
PROCESS_COUNT = 0
mutex = Lock()


# ------------------wgs84与web墨卡托互转-------------------------

# WGS-84经纬度转Web墨卡托


def wgs_to_mercator(x, y):
    y = 85.0511287798 if y > 85.0511287798 else y
    y = -85.0511287798 if y < -85.0511287798 else y

    x2 = x * 20037508.34 / 180
    y2 = math.log(math.tan((90 + y) * math.pi / 360)) / (math.pi / 180)
    y2 = y2 * 20037508.34 / 180
    return x2, y2

# Web墨卡托转经纬度

def mercator_to_wgs(x, y):
    x2 = x / 20037508.34 * 180
    y2 = y / 20037508.34 * 180
    y2 = 180 / math.pi * \
        (2 * math.atan(math.exp(y2 * math.pi / 180)) - math.pi / 2)
    return x2, y2

# -------------------------------------------------------------


# ---------------------瓦片地址到墨卡托---------------------------------
'''
东经为正，西经为负。北纬为正，南纬为负
lon经度 lat纬度 z缩放比例[0-22] ,对于卫星图并不能取到最大，测试值是20最大，再大会返回404.
山区卫星图可取的z更小，不同地图来源设置不同。
'''
# 根据WGS-84 的经纬度获取谷歌地图中的瓦片坐标


def wgs84_to_tile(lon, lat, z):
    '''
    gps              tile
         ^              -------->
         |              |
    -----|---->  =>>    |
         |              v
    Get google-style tile cooridinate from geographical coordinate
    lon : Longittude
    lat : Latitude
    z : zoom
    '''
    def isnum(x): return isinstance(x, int) or isinstance(x, float)
    if not(isnum(lon) and isnum(lat)):
        raise TypeError("lon and lat must be int or float!")

    if not isinstance(z, int) or z < 0 or z > 22:
        raise TypeError("z must be int and between 0 to 22.")

    if lon < 0:
        lon = 180 + lon
    else:
        lon += 180
    lon /= 360  # make lon to (0,1)

    lat = 85.0511287798 if lat > 85.0511287798 else lat
    lat = -85.0511287798 if lat < -85.0511287798 else lat
    lat = math.log(math.tan((90 + lat) * math.pi / 360)) / (math.pi / 180)
    lat /= 180  # make lat to (-1,1)
    lat = 1 - (lat + 1) / 2  # make lat to (0,1) and left top is 0-point

    num = 2**z
    x = math.floor(lon * num)
    y = math.floor(lat * num)
    return x, y


def tile_to_mercator(tile_x, tile_y, z):
    length = 20037508.3427892
    sum = 2**z
    mercator_x = tile_x / sum * length * 2 - length
    mercator_y = -(tile_y / sum * length * 2) + length

    return mercator_x, mercator_y

# -----------------------------------------------------------


# ---------------------度分秒转换------------------------------

def dms2dd(degrees, minutes, seconds, direction):

    dd = float(degrees) + float(minutes)/60 + float(seconds)/(60*60)

    if direction == 'E' or direction == 'N':

        dd *= -1

    return dd


def dd2dms(deg):

    d = int(deg)

    md = abs(deg - d) * 60

    m = int(md)

    sd = (md - m) * 60

    return [d, m, sd]


def parse_dms(dms):

    parts = re.split('[^\d\w]+', dms)

    lat = dms2dd(parts[0], parts[1], parts[2], parts[3])

    return (lat)


# -------------------栅格像素到墨卡托----------------------------

def imagexy2geo(trans, u, v):
    '''
    根据GDAL的六参数模型将影像图上坐标（行列号）转为投影坐标或地理坐标（根据具体数据的坐标系统转换）
    :param trans: GDAL地理转换矩阵
    :param v: 像素的行号
    :param u: 像素的列号
    :return: 行列号(v, u)对应的投影坐标或地理坐标(x, y)
    '''
    px = trans[0] + u * trans[1] + v * trans[2]
    py = trans[3] + u * trans[4] + v * trans[5]
    return px, py


def geo2imagexy(trans, x, y):
    '''
    根据GDAL的六 参数模型将给定的投影或地理坐标转为影像图上坐标（行列号）
    :param dataset: GDAL地理转换矩阵
    :param x: 投影或地理坐标x
    :param y: 投影或地理坐标y
    :return: 影坐标或地理坐标(x, y)对应的影像图上行列号(v, u)
    '''
    a = np.array([[trans[1], trans[2]], [trans[4], trans[5]]])
    b = np.array([x - trans[0], y - trans[3]])
    uv = np.linalg.solve(a, b)  # 使用numpy的linalg.solve进行二元一次方程的求解

    return int(np.floor(uv[0])), int(np.floor(uv[1]))

# -----------------GCJ02到WGS84的纠偏与互转---------------------------


def transformLat(x, y):
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * \
        y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 *
            math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 *
            math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 *
            math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def transformLon(x, y):
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + \
        0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 *
            math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 *
            math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 *
            math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def delta(lat, lon):
    '''
    Krasovsky 1940
    //
    // a = 6378245.0, 1/f = 298.3
    // b = a * (1 - f)
    // ee = (a^2 - b^2) / a^2;
    '''
    a = 6378245.0  # a: 卫星椭球坐标投影到平面地图坐标系的投影因子。
    ee = 0.00669342162296594323  # ee: 椭球的偏心率。
    dLat = transformLat(lon - 105.0, lat - 35.0)
    dLon = transformLon(lon - 105.0, lat - 35.0)
    radLat = lat / 180.0 * math.pi
    magic = math.sin(radLat)
    magic = 1 - ee * magic * magic
    sqrtMagic = math.sqrt(magic)
    dLat = (dLat * 180.0) / ((a * (1 - ee)) / (magic * sqrtMagic) * math.pi)
    dLon = (dLon * 180.0) / (a / sqrtMagic * math.cos(radLat) * math.pi)
    return {'lat': dLat, 'lon': dLon}


def outOfChina(lat, lon):
    if (lon < 72.004 or lon > 137.8347):
        return True
    if (lat < 0.8293 or lat > 55.8271):
        return True
    return False


def gcj_to_wgs(gcjLon, gcjLat):
    if outOfChina(gcjLat, gcjLon):
        return (gcjLon, gcjLat)
    d = delta(gcjLat, gcjLon)
    return (gcjLon - d["lon"], gcjLat - d["lat"])


def wgs_to_gcj(wgsLon, wgsLat):
    if outOfChina(wgsLat, wgsLon):
        return wgsLon, wgsLat
    d = delta(wgsLat, wgsLon)
    return wgsLon + d["lon"], wgsLat + d["lat"]

# --------------------------------------------------------------


# ---------------------下载器相关-----------------------------
agents = [
    'Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/60.0.3112.101 Safari/537.36',
    'Mozilla/5.0 (Windows; U; Windows NT 6.1; en-US) AppleWebKit/532.5 (KHTML, like Gecko) Chrome/4.0.249.0 Safari/532.5',
    'Mozilla/5.0 (Windows; U; Windows NT 5.2; en-US) AppleWebKit/532.9 (KHTML, like Gecko) Chrome/5.0.310.0 Safari/532.9',
    'Mozilla/5.0 (Windows; U; Windows NT 5.1; en-US) AppleWebKit/534.7 (KHTML, like Gecko) Chrome/7.0.514.0 Safari/534.7',
    'Mozilla/5.0 (Windows; U; Windows NT 6.0; en-US) AppleWebKit/534.14 (KHTML, like Gecko) Chrome/9.0.601.0 Safari/534.14',
    'Mozilla/5.0 (Windows; U; Windows NT 6.1; en-US) AppleWebKit/534.14 (KHTML, like Gecko) Chrome/10.0.601.0 Safari/534.14',
    'Mozilla/5.0 (Windows; U; Windows NT 6.1; en-US) AppleWebKit/534.20 (KHTML, like Gecko) Chrome/11.0.672.2 Safari/534.20',
    'Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/534.27 (KHTML, like Gecko) Chrome/12.0.712.0 Safari/534.27',
    'Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/535.1 (KHTML, like Gecko) Chrome/13.0.782.24 Safari/535.1'
]


class Downloader(Thread):
    # multiple threads downloader
    def __init__(self, index, PROCESS_COUNT, urls, datas, update, cache_path=None, use_cache=False, use_global_pos=True):
        # index 表示第几个线程，PROCESS_COUNT 表示线程的总数，urls 代表需要下载url列表，datas代表要返回的数据列表。
        # update 表示每下载一个成功就进行的回调函数。
        super().__init__()
        self.urls = urls
        self.datas = datas
        self.index = index
        self.PROCESS_COUNT = PROCESS_COUNT
        self.update = update
        self.pos = None
        self.cache_path = cache_path
        self.use_cache = use_cache
        self.use_global_pos = use_global_pos

    def download(self, urls):
        HEADERS = {'User-Agent': random.choice(agents)}
        # print(url)

        for full_url in urls:
            header = ur.Request(full_url, headers=HEADERS)
            try:
                # print('download ', full_url)
                # if self.pos[0] != 162 or self.pos[1] != 0:
                #     return self.pos, None
                data = ur.urlopen(header).read()
            except:
                print("\nBad network link.", full_url)
            else:
                return self.pos, data
        # raise Exception("Bad network link.")
        print("\nBad network link.", urls[0])
        return self.pos, None

    def run(self):
        for i, (x, y, tile_x, tile_y, url) in enumerate(self.urls):
            if i % self.PROCESS_COUNT != self.index:
                continue
            hit = False
            self.pos = [x, y, tile_x, tile_y]
            if self.cache_path is not None:
                pos_x = x - tile_x
                pox_y = y - tile_y
                if self.use_global_pos:
                    pos_x = x
                    pox_y = y
                name = os.path.join(
                    self.cache_path, '{}x{}.png'.format(pos_x, pox_y))

                if self.use_cache:  # 如果使用缓存直接返回数据路径，否则下载
                    if not os.path.exists(name):  # 如果已经存在缓存数据则不下载，不存在则补充下载
                        pos, data = self.download(url)
                        if data is not None:  # 如果下载不到，也没有办法
                            picio = io.BytesIO(data)
                            picpil = Image.open(picio)
                            picpil.save(name)
                            hit = True
                    else:
                        hit = True
                else:
                    pos, data = self.download(url)
                    if data is not None:  # 如果下载不到，也没有办法
                        picio = io.BytesIO(data)
                        picpil = Image.open(picio)
                        picpil.save(name)
                        hit = True

                self.datas[i] = (self.pos, name, url[0])
            else:
                pos, data = self.download(url)
                self.datas[i] = (self.pos, data, url[0])
            if mutex.acquire():
                self.update(hit)
                mutex.release()


def downTiles(urls, cache_path=None, use_cache=False, use_global_pos=True, multi=20):

    def makeupdate(s):
        def up(hit):
            global PROCESS_COUNT, HIT_COUNT
            PROCESS_COUNT += 1
            if hit:
                HIT_COUNT += 1
            print("\b"*57, end='')
            print(
                "DownLoading ... [hit:{0}/proc:{1}/total:{2}]".format(HIT_COUNT, PROCESS_COUNT, s), end='')
        return up

    url_len = len(urls)
    datas = [None] * url_len
    if multi < 1 or multi > 32 or not isinstance(multi, int):
        raise Exception(
            "multi of Downloader shuold be int and between 1 to 20.")
    tasks = [Downloader(i, multi, urls, datas, makeupdate(url_len), cache_path, use_cache, use_global_pos)
             for i in range(multi)]
    for i in tasks:
        i.start()
    for i in tasks:
        i.join()

    return datas


def geturl(source, x, y, z, style, offset):
    '''
    Get the picture's url for download.
    style:
        m for map
        s for satellite
    source:
        google or amap or tencent
    x y:
        google-style tile coordinate system
    z:
        zoom
    '''
    furls = []
    if source == 'google':
        offset = '&gl=CN' if offset else ''
        for server in range(0, 4):
            furl = MAP_URLS["google1"].format(
                server=server, x=x, y=y, z=z, style=style, offset=offset)
            furls.append(furl)
        furl = MAP_URLS["google2"].format(
            x=x, y=y, z=z, style=style, offset=offset)
        furls.append(furl)
        furl = MAP_URLS["google3"].format(
            x=x, y=y, z=z, style=style, offset=offset)
        furls.append(furl)
    elif source == 'amap':
        # for amap 6 is satellite and 7 is map.
        style = 6 if style == 's' else 8
        furl = MAP_URLS["amap"].format(x=x, y=y, z=z, style=style)
        furls.append(furl)
    elif source == 'tencent':
        y = 2**z - 1 - y
        if style == 's':
            furl = MAP_URLS["tencent_s"].format(
                x=x, y=y, z=z, fx=math.floor(x / 16), fy=math.floor(y / 16))
            furls.append(furl)
        else:
            furl = MAP_URLS["tencent_m"].format(x=x, y=y, z=z)
            furls.append(furl)
    else:
        raise Exception("Unknown Map Source ! ")

    return furls


def getTilesByBBox(bbox, zoom):
    '''
    bbox依次输入左上角的经度、纬度，右下角的经度、纬度，缩放级别，地图源，输出文件，影像类型（默认为卫星图）
    获取区域内的瓦片并自动拼合图像。返回四个角的瓦片坐标
    '''
    x1, y1, x2, y2 = bbox
    pos1x, pos1y = wgs84_to_tile(x1, y1, zoom)
    pos2x, pos2y = wgs84_to_tile(x2, y2, zoom)
    lenx = pos2x - pos1x + 1
    leny = pos2y - pos1y + 1
    print("Total number：{x} X {y}".format(x=lenx, y=leny))
    print("Pixel W x H is {} x {}".format(lenx*256, leny*256))
    mb = lenx*256 / 1024 * leny*256 / 1024 * 3
    gb = mb / 1024
    print("Maybe memory use {mb} MB or {gb} G".format(mb=mb, gb=gb))

    tiles = []
    for y in range(pos1y, pos2y+1):
        for x in range(pos1x, pos2x+1):
            tiles.append([x, y])

    return tiles, [pos1x, pos1y, pos2x, pos2y], [leny*256, lenx*256]


def getUrlsByTiles(tiles, tile_bbox, zoom, source='google', style='s', offset=False):
    pos1x, pos1y, pos2x, pos2y = tile_bbox
    urls = []
    for x, y in tiles:
        url = geturl(source, x, y, zoom, style, offset)
        urls.append([x, y, pos1x, pos1y, url])
    return urls


def getExtent(bbox, zoom, mode='tile'):
    if mode == 'tile':
        mercator_x1, mercator_y1 = tile_to_mercator(
            bbox[0], bbox[1], zoom)
        mercator_x2, mercator_y2 = tile_to_mercator(
            bbox[2]+1, bbox[3]+1, zoom)
    elif mode == 'wgs84':
        mercator_x1, mercator_y1 = wgs_to_macator(
            bbox[0], bbox[1], zoom)
        mercator_x2, mercator_y2 = wgs_to_macator(
            bbox[2]+1, bbox[3]+1, zoom)
    else:
        print("get extend error")
        return None
    return [mercator_x1, mercator_y1, mercator_x2, mercator_y2]


def getTransform(mercator_bbox, image_shape):
    height, width = image_shape
    mercator_x1, mercator_y1, mercator_x2, mercator_y2 = mercator_bbox

    # print((mercator_x2 - mercator_x1), (mercator_y2 - mercator_y1))
    res_x = (mercator_x2 - mercator_x1) / width
    res_y = (mercator_y2 - mercator_y1) / height

    trans = [mercator_x1, res_x, 0,
             mercator_y1, 0, res_y]
    return trans


def make_overview(outfile, width, height):

    filename, ext = os.path.splitext(outfile)
    out_overview_file = filename+'_overview'+ext

    dataset = gdal.Open(outfile)
    if(dataset == None):
        print("Make overview error!")
        return
    bands = dataset.RasterCount

    # driver = gdal.GetDriverByName("GTiff")
    # tods = driver.Create(out_overview_file, 1024, int(1024/width*height), 3 ,options=["INTERLEAVE=PIXEL"])
    # tods.SetGeoTransform(dataset.GetGeoTransform())
    # tods.SetProjection(dataset.GetProjection())
    # gdal.ReprojectImage(dataset, tods, dataset.GetProjection(), tods.GetProjection(), GRA_Cubic)

    datas = []
    overview_size = 1024.0
    overview_width = int(overview_size)
    overview_height = int(overview_size/width*height)
    print('Overview width x height is (%d, %d)' %
          (overview_width, overview_height))
    for i in range(bands):
        band = dataset.GetRasterBand(i+1)
        data = band.ReadAsArray(0, 0, width, height,
                                overview_width, overview_height)
        datas.append(np.reshape(data, (1, -1)))
    datas = np.concatenate(datas)
    driver = gdal.GetDriverByName("GTiff")
    tods = driver.Create(out_overview_file, overview_width,
                         overview_height, bands, options=["INTERLEAVE=PIXEL"])
    tods.WriteRaster(0, 0, overview_width, overview_height, datas.tobytes(
    ), overview_width, overview_height, band_list=[1, 2, 3])
    del tods

    del dataset


def single_save(outfile, idx, data, x, y):
    dataset = gdal.Open(outfile)
    if(dataset != None):
        dataset.GetRasterBand(idx).WriteArray(data, x, y)
        del dataset


def saveTif(datas, im_geotrans, image_shape, outfile, use_cache=False, mask=None):
    # 创建文件
    height, width = image_shape
    datatype = gdal.GDT_Byte
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(outfile, width, height, 3, datatype)
    print('save', outfile)

    f = open(outfile+'.log', 'w')

    bands = 0
    im_proj = 'PROJCS["WGS 84 / Pseudo-Mercator",GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4326"]],PROJECTION["Mercator_1SP"],PARAMETER["central_meridian",0],PARAMETER["scale_factor",1],PARAMETER["false_easting",0],PARAMETER["false_northing",0],UNIT["metre",1,AUTHORITY["EPSG","9001"]],AXIS["X",EAST],AXIS["Y",NORTH],EXTENSION["PROJ4","+proj=merc +a=6378137 +b=6378137 +lat_ts=0.0 +lon_0=0.0 +x_0=0.0 +y_0=0 +k=1.0 +units=m +nadgrids=@null +wktext +no_defs"],AUTHORITY["EPSG","3857"]]'
    if(dataset == None):
        print("Can not save %s" % outfile)
        return

    dataset.SetGeoTransform(im_geotrans)  # 写入仿射变换参数
    dataset.SetProjection(im_proj)  # 写入投影

    with ProcessPoolExecutor(max_workers=32) as executor:
        future_list = []
        for index, result in enumerate(datas):
            if result is None:
                continue

            pos, data, url = result
            x, y, tile_x, tile_y = pos

            if use_cache:
                # if data is None:
                #     f.write('read error, pos %d %d, %s\n'%(x, y, data))
                if os.path.exists(data):
                    try:
                        picpil = Image.open(data)
                        im = np.array(picpil, dtype=np.uint8)
                    except:
                        print('read %s error', data)
                        f.write('read error, pos (%d %d), tile ul(%d, %d), %s, url: %s\n' % (
                            x, y, tile_x, tile_y, data, url))
                        continue
                else:
                    f.write('read error, pos (%d, %d), tile ul(%d, %d), %s, url: %s\n' % (
                        x, y, tile_x, tile_y, data, url))
                    continue
            else:
                picio = io.BytesIO(data)
                picpil = Image.open(picio)
                im = np.array(picpil, dtype=np.uint8)
            # print(pos, data.shape)
            # print(x*256, y*256)
            _, _, bands = im.shape
            for c in range(bands):
                dataset.GetRasterBand(
                    c + 1).WriteArray(im[:, :, c], (x-tile_x)*256, (y-tile_y)*256)

        #         f1 = executor.submit(single_save, outfile, c+1, im[:,:,c], (x-tile_x)*256, (y-tile_y)*256 )
        #         future_list.append(f1)
        # for future in as_completed(future_list):
        #     future.result()

            print("\b"*50, end='')
            print(
                "Write data to tif ... [{0}/{1}]".format(index+1, len(datas)), end='')
    print()
    del dataset

    if mask is not None:
        dataset = gdal.Open(outfile)
        if(dataset != None):
            for c in range(bands):
                band_data = dataset.GetRasterBand(
                    c + 1).ReadAsArray(0, 0, width, height)
                band_data[mask == 0] = 0
                dataset.GetRasterBand(
                    c + 1).WriteArray(band_data, 0, 0)
            del dataset

    # 缩略图制作
    print('Make overview picture ...')
    make_overview(outfile, width, height)

    f.close()


def createMaskFromPoints(mercator_list, width, height):
    mask = np.zeros((height, width), np.uint8)

    for points_list in mercator_list:
        num = len(points_list)
        for idx in range(num):
            fill_value = 0
            if idx == 0:
                fill_value = 1
            else:
                fill_value = 0
            mask = cv2.fillPoly(mask, [np.array(points_list[idx])], fill_value)
    # cv2.imshow("mask", mask*255)
    # cv2.waitKey(0)
    return mask


# --------------------- 数据下载地址 ------------------------------------------


# 获取所有数据json文件

def download_Json(url, savePath):
    print("-----------正在下载json文件 %s" % (url))
    try:
        # 将响应信息进行json格式化
        # response = requests.get(url)
        # versionInfo = response.text
        # versionInfoPython = json.loads(versionInfo)

        HEADERS = {'User-Agent': random.choice(agents)}
        header = ur.Request(url, headers=HEADERS)
        try:
            data = ur.urlopen(header).read()
            versionInfoPython = json.loads(data)
            # print(versionInfo)
            path = str(savePath)
            # 将json格式化的数据保存
            with open(path, 'w', encoding='utf-8') as f1:
                f1.write(json.dumps(versionInfoPython, indent=4))
            print("下载成功，文件保存位置：" + path)
            return versionInfoPython
        except Exception as ex:
            print("Bad network link.", url, ex)

    except Exception as ex:
        print("--------下载出错----")
        print(ex)
        return None


def saveBoundaryPic(jsonFile, savePath):
    # 保存在本地的geoJson数据
    data1 = geopandas.read_file(jsonFile)

    fig, ax = plt.subplots()
    data1.plot(ax=ax, color="#FDECD2", alpha=0.8)  # 透明样式alpha=0.8
    # 绘制bbox框示意，进行重点标记（可以进行注释）
    # ax = geopandas.GeoSeries([geometry.box(minx=100,  # 红框经度（小）
    #                                        maxx=130,  # 红框经度（大）
    #                                        miny=25,  # 红框纬度（小）
    #                                        maxy=40)  # 红框纬度（大）
    #                           .boundary]).plot(ax=ax, color='red')
    plt.savefig(savePath)  # 保存图片到项目images路径下
    plt.show()


def saveShapefile(file_path, output_shapefile_name):
    try:
        data = geopandas.read_file(str(file_path))
        ax = data.plot()
        # plt.show()  # 显示生成的地图
        localPath = str(output_shapefile_name)  # 用于存放生成的文件
        data.to_file(localPath, driver='ESRI Shapefile', encoding='utf-8')
        print("--保存成功，文件存放位置："+localPath)
    except Exception as ex:
        print("--------JSON文件不存在，请检查后重试！----")
        pass


def saveInfo(infoSavePath, image_shape, mercator_bbox, trans):
    f = open(infoSavePath, 'w')
    f.write(
        "Tiles h*w: {}x{}\n".format(image_shape[0]/256, image_shape[1]/256))
    f.write("Pixel h*w: {}x{}\n".format(image_shape[0], image_shape[1]))
    f.write("Area : {} (km)^2\n".format(
        image_shape[0] / 1000 * image_shape[1] / 1000 * trans[1] * trans[1]))
    f.write("mercator_bbox ({},{}), ({},{})\n".format(
        mercator_bbox[0], mercator_bbox[1], mercator_bbox[2], mercator_bbox[3]))
    f.write("geotrans [{},{},{},{},{},{}]\n".format(
        trans[0], trans[1], trans[2], trans[3], trans[4], trans[5]))
    mb = image_shape[0] / 1024 * image_shape[1] / 1024 * 3
    gb = mb / 1024
    f.write("Maybe memory use {mb} MB or {gb} G\n".format(mb=mb, gb=gb))
    f.close()
# ---------------------2019全国行政区域shape文件读取-----------------------------


def getShpFile(shapefilename):

    shp = shapefile.Reader(shapefilename)
    border_shapes = shp.shapes()
    area_infors = shp.records()
    return border_shapes, area_infors


def initBorder():
    PROCESS_COUNTy_border_shapes, PROCESS_COUNTy_area_infors = getShpFile(
        "D:\\迅雷下载\\区划\\区划\\县.shp")
    city_border_shapes, city_area_infors = getShpFile(
        "D:\\迅雷下载\\区划\\区划\\市.shp")
    province_border_shapes, province_area_infors = getShpFile(
        "D:\\迅雷下载\\区划\\区划\\省.shp")

    return [[PROCESS_COUNTy_border_shapes, PROCESS_COUNTy_area_infors],
            [city_border_shapes, city_area_infors],
            [province_border_shapes, province_area_infors]]


def getBoarderFromDataset(name, dataset):

    for border_shapes, area_infors in dataset:
        for area_infor, border in zip(area_infors, border_shapes):
            if name in area_infor[1]:
                query_city_infor = area_infor
                query_border_infor = border
                print('query', area_infor, border.bbox)
                return query_border_infor, query_city_infor
    return None


# 使用gdal读取shapefile
def getShpFileByGDAL(shapefile, query_name):
    '''
    结果保存方式为b,c,h,w
    b为有多少个区域
    c为区域轮廓个数，例如c=1，表示只有一个外轮廓，无空洞；c=2，表示下标为0的位置为外轮廓，下标为1的位置为外轮廓，除了0以外都是外轮廓
    h为轮廓点的个数
    w为2，表示经纬度
    '''
    # 支持中文路径
    gdal.SetConfigOption("GDAL_FILENAME_IS_UTF8", "YES")
    # 支持中文编码
    gdal.SetConfigOption("SHAPE_ENCODING", "UTF-8")
    # 注册所有的驱动
    ogr.RegisterAll()
    # 打开数据
    print('打开', shapefile)
    ds = ogr.Open(shapefile, 0)
    if ds == None:
        return "打开文件失败！"

    # 获取数据源中的图层个数，shp数据图层只有一个，gdb、dxf会有多个
    iLayerPROCESS_COUNT = ds.GetLayerPROCESS_COUNT()
    print("\t图层个数 = ", iLayerPROCESS_COUNT)
    # 获取第一个图层
    result = []
    bbox = [10000, 10000, -10000, -10000]
    for layerIdx in range(iLayerPROCESS_COUNT):
        oLayer = ds.GetLayerByIndex(layerIdx)
        if oLayer == None:
            return "获取图层失败！"
        # 对图层进行初始化
        oLayer.ResetReading()
        # 输出图层中的要素个数
        num = oLayer.GetFeaturePROCESS_COUNT(0)
        print("\t要素个数 = ", num)
        result_list = []
        # 获取要素
        for i in range(0, num):
            ofeature = oLayer.GetFeature(i)
            # id = ofeature.GetFieldAsString("id")
            name = ofeature.GetFieldAsString('name')

            if query_name in name:

                PROCESS_COUNT = ofeature.GetGeometryRef().GetGeometryPROCESS_COUNT()
                for gemIdx in range(PROCESS_COUNT):
                    gemo = ofeature.GetGeometryRef().GetGeometryRef(gemIdx)
                    gemo_data = gemo.ExportToJson()
                    gemo_json = json.loads(gemo_data)
                    gemo_np = np.array(gemo_json['coordinates'])
                    print(name, gemo_np.shape)

                    min_x = min(np.min(gemo_np[..., 0]), bbox[0])
                    max_x = max(np.max(gemo_np[..., 0]), bbox[2])
                    min_y = min(np.min(gemo_np[..., 1]), bbox[1])
                    max_y = max(np.max(gemo_np[..., 1]), bbox[3])

                    bbox = [min_x, min_y, max_x, max_y]
                    result.append(gemo_np)
                # print(dir(ofeature.GetGeometryRef()),
                #       ofeature.GetGeometryRef().Boundary())

    ds.Destroy()
    del ds
    return result, bbox


def downloadRectDemo(geo_name, left, top, right, bottom, source='google', style='s', zoom=12, offset=False, cache_path=None, use_cache=True, outPath='.', use_global_pos=False, force_save=True):
    # source = 'google'
    # style = 's'
    # zoom = 18
    # offset = False
    # geo_name = 'test'
    global PROCESS_COUNT, HIT_COUNT
    PROCESS_COUNT = 0
    HIT_COUNT = 0
    if cache_path is not None:
        if use_global_pos:
            cache_path = os.path.join(cache_path, '%s_%d' % (source, zoom))
        else:
            cache_path = os.path.join(
                cache_path, '%s_%s_%d' % (name, source, zoom))
        if not os.path.exists(cache_path):
            os.makedirs(cache_path)

    ulx = parse_dms(left)
    uly = parse_dms(top)
    lrx = parse_dms(right)
    lry = parse_dms(bottom)

    gps_bbox = [ulx, uly, lrx, lry]

    # gps_bbox = [120.09198099000002, 27.176422685000115,
    #             120.09819027500012, 27.173431772500086]

    tiles, tile_bbox, image_shape = getTilesByBBox(gps_bbox, zoom)
    urls = getUrlsByTiles(tiles, tile_bbox, zoom, source, style, offset)

    mercator_bbox = getExtent(tile_bbox, zoom, mode='tile')
    trans = getTransform(mercator_bbox, image_shape)
    print('mercator bbox is', mercator_bbox)
    print('geotransform is', trans)
    # print(image_shape)
    saveInfo(infoSavePath, image_shape, mercator_bbox, trans)

    datas = downTiles(urls, cache_path=cache_path,
                      use_cache=use_cache, use_global_pos=use_global_pos, multi=20)

    print("\nDownload Finished！ Pics (w,h) is (%d,%d) Mergeing......" %
          (image_shape[0], image_shape[1]))

    if HIT_COUNT != PROCESS_COUNT:
        print("DownLoading %d but total %d !!!!" % (HIT_COUNT, PROCESS_COUNT))
        if not force_save:
            print("Shutdown !!!!")
            return

    gl = 'gl' if offset else 'nogl'
    outfile = '{}_{}{}_{}_{}.tif'.format(source, zoom, style, gl, geo_name)
    outfile = os.path.join(outPath, outfile)
    use_cache = True if cache_path is not None else False
    saveTif(datas, trans, image_shape, outfile, use_cache=use_cache)

    # if os.path.exists(cache_path):
    #     shutil.rmtree(cache_path)


def downloadShpDemo():
    source = 'google'
    style = 's'
    zoom = 18
    offset = False
    geo_name = '苍南县'
    PROCESS_COUNT = 0
    datasets = initBorder()
    boarders = getBoarderFromDataset(geo_name, datasets)
    if boarders is None:
        print('not find', geo_name)
        return

    query_border_infor, query_city_infor = boarders

    geo_bbox = query_border_infor.bbox
    w_lon = geo_bbox[0]
    n_lat = geo_bbox[3]
    e_lon = geo_bbox[2]
    s_lat = geo_bbox[1]

    gps_bbox = [w_lon, n_lat,
                e_lon, s_lat]
    print(gps_bbox)
    gps_bbox = [120.09198099000002, 27.176422685000115,
                120.09819027500012, 27.173431772500086]

    tiles, tile_bbox, image_shape = getTilesByBBox(gps_bbox, zoom)
    urls = getUrlsByTiles(tiles, tile_bbox, zoom, source, style, offset)

    mercator_bbox = getExtent(tile_bbox, zoom, mode='tile')
    trans = getTransform(mercator_bbox, image_shape)
    print('mercator bbox is', mercator_bbox)
    print('geotransform is', trans)
    datas = downTiles(urls)

    print("\nDownload Finished！ Pics (w,h) is (%d,%d) Mergeing......" %
          (image_shape[0], image_shape[1]))

    gl = 'gl' if offset else 'nogl'
    outfile = '{}_{}{}_{}_{}.tif'.format(source, zoom, style, gl, geo_name)
    saveTif(datas, trans, image_shape, outfile)


def downloadShpDemoWithMask():

    source = 'google'
    style = 's'
    zoom = 12
    offset = False
    geo_name = '苍南县'
    PROCESS_COUNT = 0
    boundary, geo_bbox = getShpFileByGDAL("D:\\迅雷下载\\区划\\区划\\县.shp", geo_name)
    # boundary, geo_bbox = getShpFileByGDAL("D:\\迅雷下载\\区划\\区划\\市.shp", geo_name)
    # boundary, geo_bbox = getShpFileByGDAL("D:\\迅雷下载\\区划\\区划\\省.shp", geo_name)

    w_lon = geo_bbox[0]
    n_lat = geo_bbox[3]
    e_lon = geo_bbox[2]
    s_lat = geo_bbox[1]

    gps_bbox = [w_lon, n_lat,
                e_lon, s_lat]
    print(gps_bbox)
    # gps_bbox = [120.09198099000002, 27.176422685000115,
    #             120.09819027500012, 27.173431772500086]

    tiles, tile_bbox, image_shape = getTilesByBBox(gps_bbox, zoom)
    # print(tiles, tile_bbox, image_shape)
    urls = getUrlsByTiles(tiles, tile_bbox, zoom, source, style, offset)

    mercator_bbox = getExtent(tile_bbox, zoom, mode='tile')
    trans = getTransform(mercator_bbox, image_shape)
    print('mercator bbox is', mercator_bbox)
    print('geotransform is', trans)
    datas = downTiles(urls)

    print("\nDownload Finished！ Pics (w,h) is (%d,%d) Mergeing......" %
          (image_shape[0], image_shape[1]))

    # 将wgs84转为墨卡托坐标
    mercator_boundary_list = []
    for points_list in boundary:
        mercator_points_list = []
        for points in points_list:
            mercator_points = []
            for point in points:
                x, y = wgs_to_mercator(point[0], point[1])
                x, y = geo2imagexy(trans, x, y)
                mercator_points.append([x, y])
            mercator_points_list.append(mercator_points)
        mercator_boundary_list.append(mercator_points_list)

    mask = createMaskFromPoints(
        mercator_boundary_list, image_shape[1],  image_shape[0])

    gl = 'gl' if offset else 'nogl'
    outfile = '{}_{}{}_{}_{}.tif'.format(source, zoom, style, gl, geo_name)
    saveTif(datas, trans, image_shape, outfile, mask=mask)

# url：geojson路径, name：保存名字, source：数据源, style：卫星影像或是地图, zoom：等级, offset：是否偏移, use_mask：是否使用掩码
# gcj2wgs：如果有偏移是否转换, outPath：输出路径, cache_path：缓存路径, use_cache：是否使用缓存数据, include_list：geojson中包含多个城市，使用哪几个城市
# use_global_pos： 缓存时是否使用全球瓦片坐标, force_save：下载不完全，是否强制写


def downloadJsonDemo(url, name, source='google', style='h', zoom=18, offset=False, use_mask=False, gcj2wgs=True, outPath='.', cache_path=None, use_cache=True, use_global_pos=False, include_list=[], force_save=True):
    # 从datav中查找, 无偏移的，offset一定要设置为true
    # url = 'https://geo.datav.aliyun.com/areas_v3/bound/330383.json'
    # name = '龙港市'

    # source = 'google'
    # style = 's'
    # zoom = 12
    # offset = False
    geo_name = name
    # use_mask = True  # 是否使用mask
    # gcj2wgs = True  # url是无偏移，所以如果要下有偏移的，就先把gps从gcj转回wgs
    global PROCESS_COUNT, HIT_COUNT
    PROCESS_COUNT = 0
    HIT_COUNT = 0
    if cache_path is not None:
        if use_global_pos:
            cache_path = os.path.join(cache_path, '%s_%d' % (source, zoom))
        else:
            cache_path = os.path.join(
                cache_path, '%s_%s_%d' % (name, source, zoom))
        if not os.path.exists(cache_path):
            os.makedirs(cache_path)
            print('创建文件夹', cache_path)

    saveDir = '{}_{}'.format(name, time.strftime(
        '%Y%m%d%H%M%S', time.localtime(time.time())))
    saveDir = os.path.join(outPath, saveDir)
    if not os.path.exists(saveDir):
        os.makedirs(saveDir)
        print('创建文件夹', saveDir)

    jsonSavePath = saveDir+'\\'+name+'.json'
    pngSavePath = saveDir+'\\'+name+'.png'
    infoSavePath = saveDir+'\\'+name+'.xml'
    shpSavePath = saveDir+'\\'+name
    cityJson = download_Json(url, jsonSavePath)
    if cityJson is None:
        return

    # saveBoundaryPic(jsonSavePath, pngSavePath)
    saveShapefile(jsonSavePath, shpSavePath)

    geo_bbox = [1000, 1000, -1000, -1000]

    for cityItem in cityJson['features']:

        if cityItem['properties']['name'] not in include_list:
            continue
        print('adcode:{}, name:{},center:[{},{}],centeroid:[{},{}],parent adcode:{}'.format(cityItem['properties']['adcode'], cityItem['properties']['name'], cityItem['properties']
                                                                                            ['center'][0], cityItem['properties']['center'][1], cityItem['properties']['centroid'][0], cityItem['properties']['centroid'][1], cityItem['properties']['parent']['adcode']))

        boundary = np.array(cityItem['geometry']['coordinates'])
        for points_list in boundary:
            for points in points_list:
                for point in points:
                    geo_bbox[0] = min(geo_bbox[0], point[0])
                    geo_bbox[1] = min(geo_bbox[1], point[1])
                    geo_bbox[2] = max(geo_bbox[2], point[0])
                    geo_bbox[3] = max(geo_bbox[3], point[1])

    if gcj2wgs:
        geo_bbox[0], geo_bbox[1] = gcj_to_wgs(geo_bbox[0], geo_bbox[1])
        geo_bbox[2], geo_bbox[3] = gcj_to_wgs(geo_bbox[2], geo_bbox[3])
    print('geobox', geo_bbox)

    w_lon = geo_bbox[0]
    n_lat = geo_bbox[3]
    e_lon = geo_bbox[2]
    s_lat = geo_bbox[1]

    gps_bbox = [w_lon, n_lat,
                e_lon, s_lat]
    print('gps_bbox', gps_bbox)

    tiles, tile_bbox, image_shape = getTilesByBBox(gps_bbox, zoom)
    # print(tiles, tile_bbox, image_shape)
    urls = getUrlsByTiles(tiles, tile_bbox, zoom, source, style, offset)

    mercator_bbox = getExtent(tile_bbox, zoom, mode='tile')
    trans = getTransform(mercator_bbox, image_shape)
    print('mercator bbox is', mercator_bbox)
    print('geotransform is', trans)
    saveInfo(infoSavePath, image_shape, mercator_bbox, trans)

    datas = downTiles(urls, cache_path=cache_path,
                      use_cache=use_cache, use_global_pos=use_global_pos, multi=20)

    print("\nDownload Finished！ Pics (w,h) is (%d,%d) Mergeing......" %
          (image_shape[0], image_shape[1]))

    # # 将wgs84转为墨卡托坐标
    # mercator_boundary_list = []
    # for points_list in boundary:
    #     mercator_points_list = []
    #     for points in points_list:
    #         mercator_points = []
    #         for point in points:
    #             if gcj2wgs:
    #                 point[0], point[1] = gcj_to_wgs(point[0], point[1])
    #             x, y = wgs_to_mercator(point[0], point[1])
    #             x, y = geo2imagexy(trans, x, y)
    #             mercator_points.append([x, y])
    #         mercator_points_list.append(mercator_points)
    #     mercator_boundary_list.append(mercator_points_list)

    # mask = None
    # if use_mask:
    #     mask = createMaskFromPoints(
    #         mercator_boundary_list, image_shape[1],  image_shape[0])

    if HIT_COUNT != PROCESS_COUNT:
        print("DownLoading %d but total %d !!!!" % (HIT_COUNT, PROCESS_COUNT))
        if not force_save:
            print("Shutdown !!!!")
            return
    # print(HIT_COUNT, PROCESS_COUNT)

    gl = 'gl' if offset else 'nogl'
    outfile = '{}/{}_{}{}_{}_{}.tif'.format(saveDir,
                                            source, zoom, style, gl, geo_name)
    use_cache = True if cache_path is not None else False
    saveTif(datas, trans, image_shape, outfile, use_cache=use_cache, mask=None)

    # if os.path.exists(cache_path):
    #     shutil.rmtree(cache_path)


# https://datav.aliyun.com/portal/school/atlas/area_selector
if __name__ == "__main__":
    # downloadRectDemo()
    # downloadShpDemoWithMask()
    # downloadRectDemo('test', left='11^11^22.33N', top='11^11^22.33N', right='11^11^22.33N', bottom='11^11^22.33N', source = 'google', style = 's', zoom=12, offset = False, outPath='I:\\remote', zoom=18, cache_path='I:\\remote\\cache1', use_cache=True, use_global_pos=False, force_save=False)

    downloadJsonDemo(name='深圳市_南山区', url='https://geo.datav.aliyun.com/areas_v3/bound/geojson?code=440300_full', outPath='D:\\remote',
                     zoom=12, cache_path='D:\\remote\\cache1', style='m',source='amap', use_global_pos=True, force_save=True, include_list=['南山区'])
