# // Construct URL to image
# let url = 'http://webst02.is.autonavi.com/appmaptile'
# url += '?style=6'
# url += '&x=${x}'
# url += '&y=${y}'
# url += '&z=${zoom}'
#
# // Request the image
# let image = await fetch(url).then(response => response.blob());


import urllib.request
import json
# 高德API
url = 'http://restapi.amap.com/v3/staticmap'

# 参数信息
# 假设：经度117.18756，纬度39.14393，缩放级别14
parameters = {
    'location':'117.18756,39.14393',
    'zoom':'14',
    'scale':'2',
    'size':'400*400',
    'key':'62bc4994360db7aa57992d1ef39583e6',
    'sco':'satellite'
}
# 生成url
url = url + '?' + urllib.parse.urlencode(parameters)
# 将url编码，以便发送请求
url = urllib.parse.quote(url, safe="?&=/,:@+$")
# 获取返回的图片url
response = urllib.request.urlopen(url).read().decode()

# 解析json
res_dict = json.loads(response)

# 获取图片URL
img_url = res_dict['url']

# 下载图片
urllib.request.urlretrieve(img_url, 'satellite.png')