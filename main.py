# -*- coding: utf-8 -*-

import os, sys
import datetime, random, json, base64, cStringIO
import logging, ConfigParser
import numpy as np
from flask import Flask, render_template, request, redirect, url_for
from flask import send_from_directory
from PIL import Image, ImageDraw, ImageFont
import gspread, cloudstorage
from oauth2client.service_account import ServiceAccountCredentials
from google.appengine.api import app_identity

scope = [u'https://spreadsheets.google.com/feeds',
         u'https://www.googleapis.com/auth/drive']

credentials = ServiceAccountCredentials.from_json_keyfile_name('tana-f451e6efe53e.json', scope)

# アプリケーション設定読込
config = ConfigParser.ConfigParser()
config.read('setting.ini')
 
google_settings = 'google_settings'
workbook_key = config.get(google_settings, 'workbook') # 使用するワークブックのキー
master_sheet_name = config.get(google_settings, 'master_name') # マスタ用シート名
result_sheet_name = config.get(google_settings, 'result_name') # 結果出力用シート名
result_image_dir = config.get(google_settings, 'result_dir') # クラウドストレージ保存フォルダ
gc_prefix = config.get(google_settings, 'gc_prefix')         # クラウドストレージ参照URL
bucket_name = config.get(google_settings, 'bucket_name')

# cloud storage設定
retryparams_instance = cloudstorage.RetryParams(initial_delay=0.2, max_delay=5.0, backoff_factor=2, max_retry_period=15)
cloudstorage.set_default_retry_params(retryparams_instance)


app = Flask(__name__)

# google cloud storageへ画像アップロード
def writeCloudStorage(image_name, image_data):

    if os.path.splitext(image_name)[1].lower() == '.jpg':
        mimetype = 'image/jpeg'
    elif os.path.splitext(image_name)[1].lower() == '.png':
        mimetype = 'image/png'
    else:
        mimetype = ''

    # cloud storage書込
    retryparams_w = cloudstorage.RetryParams(backoff_factor=1.1)
    cloudstorage_file = cloudstorage.open(filename='/' + bucket_name + '/' + result_image_dir + '/' + image_name,
        mode='w',
        content_type = mimetype,
        retry_params=retryparams_w)
    cloudstorage_file.write(image_data)
    cloudstorage_file.close()


# SpreadSheet ブック取得
def loadWorkBook(credentials, workbook_key):
    gc = gspread.authorize(credentials)
    return gc.open_by_key(workbook_key)


# spradの商品マスタ読込
def loadMaster():
    wb = loadWorkBook(credentials, workbook_key)
    master_sheet = wb.worksheet(master_sheet_name)

    master_data = []
    for i in range(100):
        row = []
        if master_sheet.cell(i+2, 1).value == '':
            break
        row.append(master_sheet.cell(i+2, 1).value) # 商品コード
        row.append(master_sheet.cell(i+2, 2).value) # 商品名
        row.append(0)                               # 数量(カウント用)
        master_data.append(row)

    return master_data


# spradの商品マスタ書込
def writeMaster(product_code, product_name):
    wb = loadWorkBook(credentials, workbook_key)
    master_sheet = wb.worksheet(master_sheet_name)
    master_sheet.append_row([product_code, product_name])


# spreadへの推論結果出力
def writeResult(result, predict_datetime, result_image_name):
    wb = loadWorkBook(credentials, workbook_key)
    result_sheet = wb.worksheet(result_sheet_name)

    # cloud storageのURLに変換
    image_url = gc_prefix + '/' + bucket_name + '/' + result_image_dir + '/' + result_image_name

    for j in range(len(result)):
        row = [predict_datetime, result[j, 0], result[j, 1], result[j, 2], image_url]
        result_sheet.append_row(row)


# 元画像と推論結果からプレビューイメージ取得
def getResultImg(source_img, od_result, result_list):
    draw = ImageDraw.Draw(source_img)

    #colors= {'1001':(255, 0, 0), '1002':(0, 255, 0), '1003':(0, 0, 255), '1004':(255, 255, 0)}

    # 商品ごとにランダムで色生成
    colors = dict()
    for i in range(len(result_list)):
        colors[result_list[i][0]] = (random.randint(0, 200), random.randint(0, 200), random.randint(0, 200))

    font = ImageFont.truetype("ipaexg.ttf", 30) #ラベルフォントサイズ設定

    # ボックス、ラベルの描画
    for obj in od_result['regions']:
        color = colors[obj['tags'][0]]
        point1 = (int(obj['points'][0]['x']), int(obj['points'][0]['y']))
        point2 = (int(obj['points'][1]['x']), int(obj['points'][1]['y']))
        point3 = (int(obj['points'][2]['x']), int(obj['points'][2]['y']))
        point4 = (int(obj['points'][3]['x']), int(obj['points'][3]['y']))
        # バウンディングボックス描画
        draw.line((point1, point2), fill=color, width=10)
        draw.line((point2, point3), fill=color, width=10)
        draw.line((point3, point4), fill=color, width=10)
        draw.line((point1, point4), fill=color, width=10)
        # ラベル描画
        draw.rectangle((point1[0], point1[1], point1[0]+150, point1[1]+50), fill=color)
        draw.text((point1[0]+5, point1[1]+5), obj['tags'][0], fill=(255, 255, 255), font=font)

    buf = cStringIO.StringIO()
    source_img.save(buf, 'JPEG')
    img_b64str = base64.b64encode(buf.getvalue())
    img_b64data = "data:image/jpg;base64,{}".format(img_b64str)

    return buf.getvalue(), img_b64data


# 推論結果から商品ごとの集計を取得
def getResultList(od_result):

    products = loadMaster()

    for obj in od_result['regions']:
        for i in range(4):
            if obj['tags'][0] == products[i][0]:
                products[i][2] += 1

    return products


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/result/', methods=['POST'])
def result():
    #アップロードされた画像の処理
    upload_file = request.files['image-upload']
    upload_file_name = None
    if upload_file.filename is not None:
        upload_file_name = upload_file.filename

    #現在時刻を取得(ファイル名、結果出力用)
    now = datetime.datetime.now()
    now = now + datetime.timedelta(hours=9)
    predict_datetime = now.strftime('%Y/%m/%d %H:%M:%S')

    # テスト用固定画像、推論結果ファイルロード(テスト用)
    source_img = Image.open('DSC_0200.JPG')
    a = open('annot.json')
    od_result = json.load(a)

    # 推論結果を商品別に集計
    result_list = getResultList(od_result)
    # 元画像+推論結果でボックス表示画像取得
    result_image, result_image_base64 = getResultImg(source_img, od_result, result_list)

    # cloud storage書込
    result_file_name = now.strftime('%Y%m%d%H%M%S') + '_' + upload_file_name 
    writeCloudStorage(result_file_name, result_image)

    return render_template('result.html', result_image=result_image_base64,
         result_list=result_list,
         result_file_name = result_file_name,
         predict_datetime = predict_datetime)


@app.route('/regist/', methods=["POST"])
def regist_result():
    # 結果画面の集計編集結果を読み込み
    predict_datetime = request.form['predict-datetime']  # 推論時間(hidden)
    result_file_name = request.form['result-file-name']  # 推論結果ファイル名
    product_codes = request.form.getlist('product-code') # 商品コード(list)
    product_names = request.form.getlist('product-name') # 商品名(list)
    detect_nums = request.form.getlist('detect-num')     # 検出数量(list)

    result = np.c_[product_codes, product_names, detect_nums]
    writeResult(result, predict_datetime, result_file_name)

    return render_template('regist_done.html')


@app.route('/regist-master/', methods=["POST"])
def regist_master():
    #image_file = request.files['image-upload']
    #annot_file = request.files['master-annot']
    # ここで学習をキックする(はず)
    product_code = request.form['product-code']
    product_name = request.form['product-name']
    writeMaster(product_code, product_name)
    return render_template('regist_done.html')


@app.route('/master/')
def master():
	return render_template('master.html')


@app.errorhandler(500)
def server_error(e):
    # Log the error and stacktrace.
    logging.exception('An error occurred during a request.')
    return 'An internal error occurred.', 500
