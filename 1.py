import uvicorn
from pydantic import BaseModel
from fastapi import FastAPI,Header,Form
from typing import List,Optional
from fastapi.responses import HTMLResponse,FileResponse
import requests
import logging
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO,filename="log.txt",filemode="a",format="%(asctime)s - %(pathname)s[line:%(lineno)d] - %(levelname)s: %(message)s")
app = FastAPI()
app.mount("/templates", StaticFiles(directory="./templates"), name="templates")

#解决cors跨域问题
@app.middleware("http")
async def add_process_time_header(request, call_next):
    response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    response.headers["Access-Control-Max-Age"] = "1000"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response
    

@app.get("/buff/{goods_id}")
def get_buff(goods_id:str):
    '''
    返回buff商品信息
    code 200 正常
    code 404 未找到该商品或者出错了
    '''
    try:
        url = "https://buff.163.com/api/market/goods/sell_order?game=csgo&goods_id={}&page_num=1&sort_by=default&mode=&allow_tradable_cooldown=1&_=1672670602560".format(goods_id);
        headers = {
            "referer" : "https://buff.163.com/goods/{}".format(goods_id),
            "user-agent" : "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
        }
        r = requests.get(url,headers=headers)
        _json = r.json()
        if(_json["code"] != "OK"):
            data = {
                "code" : 404,
                "msg" : "未找到该商品或者出错了"
            }
            logging.error("获取buff商品信息失败:{}".format(data))
            return data
        sb_price = []
        for size in range(0,5):
            sb_price.append(_json["data"]["items"][size]["price"])
        data = {
            "code" : 200,
            "msg" : {
                "商品名称" : _json["data"]["goods_infos"][str(goods_id)]["name"],
                "steamCNY价格" : _json["data"]["goods_infos"][str(goods_id)]["steam_price_cny"],
                "玩家价格前五" : sb_price
            }
        }
        logging.info("成功获取buff商品信息:{}".format(data))
        return data
    except:
        data = {
            "code" : 404,
            "msg" : "未找到该商品或者出错了"
        }
        logging.error("获取buff商品信息失败:{}".format(data))
        return data

@app.post("/submit/{goods_id}")
async def submit(goods_id):
    '''
    提交buff商品id
    code 200 正常
    code 400 重复提交
    code 404 出错
    '''
    #先判断是否存在商品
    _json = get_buff(goods_id)
    if _json["code"] == 404:
        logging.error("提交失败:{}".format(_json["msg"]))
        return {"code": 404, "msg": "提交失败"}
    fileName = "script/submit.buff"
    with open(fileName,"a+") as f:
        # 判断是否有重复
        f.seek(0)
        for line in f.readlines():
            if goods_id in line:
                logging.error("重复提交:{}".format(goods_id))
                return {"code": 400, "msg": "重复提交"}
        f.write(goods_id + "\n")
        logging.info("提交成功:{}".format(goods_id))
    f.close()
    return {"code": 200, "msg": "提交成功"}

@app.get("/get")
def get():
    '''
    根据文件获取buff商品信息返回json
    调用get_buff
    '''
    fileName = "script/submit.buff"
    Json = [{"data":{}}]
    Json[0]["code"] = 0
    cnt = 0
    with open(fileName,"r") as f:
        for line in f.readlines():
            line = line.strip()
            if line:
                _json = get_buff(line)
                if _json["code"] == 200:
                    Json[0]["data"][cnt] = _json["msg"]
                    cnt += 1
    Json[0]["count"] = cnt

    f.close()
    return Json.pop()

@app.get("/")
def index():
    #打开templates/index.html
    return HTMLResponse(open("templates/index.html","r").read())

if __name__ == "__main__":
    uvicorn.run(app='1:app', host="127.0.0.1", port=8000, reload=True)