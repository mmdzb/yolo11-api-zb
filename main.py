from fastapi import FastAPI, File, UploadFile, Query
from fastapi.responses import JSONResponse
from ultralytics import YOLO
from fastapi.responses import StreamingResponse
from ultralytics.utils.plotting import Annotator, colors
from PIL import Image
import numpy as np
import pandas as pd
import cv2
import io
import time

from prometheus_client import start_http_server, Gauge, Counter, Histogram

# 定义 Prometheus 指标
time_gauge = Gauge('response_time', 'Average response time of the PyTorch model')
request_number = Counter('request_number', 'The number of predict requests')
confidence_distribution = Histogram('confidence_distribution', 'Confidence distribution of predictions.', buckets=[0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
# confidence_gauge = Gauge('confidence', 'Training accuracy of the PyTorch model')

start_http_server(8000)

app = FastAPI(
    title="YOLO11 Deployment API",
    description="A RESTful API for deploying Ultralytics YOLO11 model(s)",
    version="1.0"
)

# -------------------------
# 1. Load Your YOLO11 Model(s)
# -------------------------
# Assuming you have at least one model in the ./models directory.
# For demonstration, we load two models (they could be the same file or different).
model_0 = YOLO("./models/best.pt")
model_1 = YOLO("./models/v1.pt")  # For example, if you have a second model

models = {
    "model_0": model_0,
    "model_1": model_1
}

default_model_name = "model_0"  # Default model to use if none is specified
start_time = time.time()

# Variables for simple metrics tracking
request_count = 0
total_latency = 0.0
max_latency = 0.0
request_timestamps = []  # list to track request times for request rate calculation

# -------------------------
# 2. Helper Functions
# -------------------------
def get_uptime():
    elapsed = time.time() - start_time
    days = int(elapsed // 86400)
    hours = int((elapsed % 86400) // 3600)
    minutes = int((elapsed % 3600) // 60)
    return f"{days} days, {hours} hours, {minutes} minutes"

def process_image(image_bytes: bytes) -> np.ndarray:
    """Converts raw image bytes into a NumPy image (BGR) using OpenCV."""
    np_arr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    return img

def add_bboxs_on_img(image: Image, predict) -> Image:
    
    annotator = Annotator(np.array(image))

    # sort predict by xmin value
    predict = predict.sort_values(by=['xmin'], ascending=True)

    # iterate over the rows of predict dataframe
    for i, row in predict.iterrows():
        # create the text to be displayed on image
        text = f"{row['name']}: {int(row['confidence']*100)}%"
        # get the bounding box coordinates
        bbox = [row['xmin'], row['ymin'], row['xmax'], row['ymax']]
        # add the bounding box and text on the image
        annotator.box_label(bbox, text, color=colors(row['class'], True))
    # convert the annotated image to PIL image
    return Image.fromarray(annotator.result())

def get_bytes_from_image(image: Image) -> bytes:
    return_image = io.BytesIO()
    image.save(return_image, format='JPEG', quality=85)  # save the image in JPEG format with quality 85
    return_image.seek(0)  # set the pointer to the beginning of the file
    return return_image

def transform_predict_to_df(results: list, labeles_dict: dict) -> pd.DataFrame:
    
    # Transform the Tensor to numpy array
    predict_bbox = pd.DataFrame(results[0].to("cpu").numpy().boxes.xyxy, columns=['xmin', 'ymin', 'xmax','ymax'])
    # Add the confidence of the prediction to the DataFrame
    predict_bbox['confidence'] = results[0].to("cpu").numpy().boxes.conf
    # Add the class of the prediction to the DataFrame
    predict_bbox['class'] = (results[0].to("cpu").numpy().boxes.cls).astype(int)
    # Replace the class number with the class name from the labeles_dict
    predict_bbox['name'] = predict_bbox["class"].replace(labeles_dict)
    return predict_bbox

# -------------------------
# 3. API Endpoints
# -------------------------

# Health Check Endpoint
@app.get("/health-status")
def health_status():
    return {
        "status": "Healthy",
        "server": "FastAPI",
        "uptime": get_uptime()
    }

# Model Management Endpoint (List Models)
@app.get("/management/models")
def list_models():
    return {
        "available_models": list(models.keys())
    }

# Group Info Endpoint
@app.get("/group-info")
def group_info():
    return {
        "group": "Group 2",
        "members": ["Fuad Hasan", "Junfeng Lei", "Zhibo Wang", "Tony Zhao"]
    }

# Model Info Endpoint (Describe a Model)
@app.get("/management/models/{model}/describe")
def describe_model(model: str):
    dummy_configs = {
        "model_0": {
            "input_size": [640, 640],
            "batch_size": 16,
            "confidence_threshold": 0.25
        },
        "model_1": {
            "input_size": [640, 640],
            "batch_size": 16,
            "confidence_threshold": 0.30
        }
    }
    if model not in models:
        return JSONResponse(status_code=404, content={"error": "Model not found."})
    return {
        "model": model,
        "config": dummy_configs.get(model, {}),
        "date_registered": "2025-03-26"  # This can be dynamically set if desired
    }

# Change Default Model Endpoint
@app.get("/management/models/{model}/set-default")
def set_default_model(model: str):
    global default_model_name
    if model not in models:
        return JSONResponse(status_code=404, content={"error": "Model not found."})
    default_model_name = model
    return {
        "success": True,
        "default_model": default_model_name
    }

# Inference Endpoint (/predict)
@app.post("/predict")
def predict(image: UploadFile = File(...), model: str = Query(None, description="YOLO model name to use")):
    global request_count, total_latency, max_latency

    start_request_time = time.time()
    request_timestamps.append(start_request_time)
    
    # Choose model
    if model and model in models:
        chosen_model = models[model]
        chosen_model_name = model
    else:
        chosen_model = models[default_model_name]
        chosen_model_name = default_model_name

    # Read image file from request
    contents = image.file.read()
    img = process_image(contents)
    
    # Run inference using Ultralytics YOLO11 predict mode
    # Here, stream=False returns a list of Results objects
    results = chosen_model.predict(img, conf=0.25, imgsz=640)
    
    predictions = []
    # Loop through results (for each image; typically one image per request)
    for r in results:
        # Each result contains a Boxes object
        for box in r.boxes:
            # Extract box coordinates in xyxy format, confidence, and class index
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            conf = float(box.conf[0])
            cls_id = int(box.cls[0])
            label = chosen_model.names[cls_id]
            predictions.append({
                "label": label,
                "confidence": round(conf, 2),
                "bbox": [x1, y1, x2, y2]
            })

    elapsed = time.time() - start_request_time
    request_count += 1
    total_latency += elapsed
    if elapsed > max_latency:
        max_latency = elapsed

    time_gauge.set(total_latency/request_count)
    request_number.inc()
    confidence_distribution.observe(predictions[0]["confidence"])

    return {
        "predictions": predictions,
        "model_used": chosen_model_name
    }
    
@app.post("/predict_visualization")
def predict_visualization(image: UploadFile = File(...), model: str = Query(None, description="YOLO model name to use")):
    """
    Object Detection from an image plot bbox on image

    Args:
        file (bytes): The image file in bytes format.
    Returns:
        Image: Image in bytes with bbox annotations.
    """
    global request_count, total_latency, max_latency

    start_request_time = time.time()
    request_timestamps.append(start_request_time)
    
    # Choose model
    if model and model in models:
        chosen_model = models[model]
        chosen_model_name = model
    else:
        chosen_model = models[default_model_name]
        chosen_model_name = default_model_name

    # Read image file from request
    contents = image.file.read()
    img = process_image(contents)
    
    # Run inference using Ultralytics YOLO11 predict mode
    # Here, stream=False returns a list of Results objects
    results = chosen_model.predict(img, conf=0.25, imgsz=640)
    results = transform_predict_to_df(results, chosen_model.model.names)

    # add bbox on image
    final_image = add_bboxs_on_img(image = img, predict = results)

    # return image in bytes format
    return StreamingResponse(content=get_bytes_from_image(final_image), media_type="image/jpeg")

# Metrics Endpoint
@app.get("/metrics")
def get_metrics():
    global request_count, total_latency, max_latency
    now = time.time()
    one_min_ago = now - 60
    while request_timestamps and request_timestamps[0] < one_min_ago:
        request_timestamps.pop(0)
    request_rate = len(request_timestamps)
    avg_latency = (total_latency / request_count * 1000) if request_count else 0.0
    max_latency_ms = max_latency * 1000
    return {
        "request_rate_per_minute": request_rate,
        "avg_latency_ms": round(avg_latency, 2),
        "max_latency_ms": round(max_latency_ms, 2),
        "total_requests": request_count
    }

# -------------------------
# 4. Run the Application
# -------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
