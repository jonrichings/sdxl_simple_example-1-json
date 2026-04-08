import base64, json, random, time, os
from io import BytesIO

import requests
import runpod
from PIL import Image

COMFY_URL = "http://127.0.0.1:8188"

DEFAULTS = {
    "prompt": "make the sky red",
    "negative_prompt": "text, watermark, animals, clouds, boats, people.",
    "seed": -1,

    "steps_base": 20,
    "cfg_base": 8.0,
    "sampler_base": "euler",
    "scheduler_base": "simple",
    "denoise": 0.30,

    "steps_refiner": 25,
    "cfg_refiner": 8.0,
    "sampler_refiner": "euler",
    "scheduler_refiner": "normal",
    "refiner_start_at_step": 20,

    "jpeg_quality": 90
}

def get(inp, k): return inp.get(k, DEFAULTS[k])

def fetch_bytes(url: str) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; RunpodServerless/1.0)"}
    r = requests.get(url, headers=headers, timeout=120)
    r.raise_for_status()
    return r.content

def to_png_bytes(image_bytes: bytes) -> bytes:
    im = Image.open(BytesIO(image_bytes)).convert("RGB")
    buf = BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()

def comfy_upload_image(png_bytes: bytes, filename="input.png") -> str:
    files = {"image": (filename, png_bytes, "image/png")}
    r = requests.post(f"{COMFY_URL}/upload/image", files=files, timeout=60)
    r.raise_for_status()
    return r.json()["name"]

def comfy_submit(workflow: dict) -> str:
    r = requests.post(f"{COMFY_URL}/prompt", json={"prompt": workflow}, timeout=60)
    r.raise_for_status()
    return r.json()["prompt_id"]

def comfy_wait_history(prompt_id: str, timeout_s=600) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=60)
        r.raise_for_status()
        hist = r.json().get(prompt_id)
        if hist and "outputs" in hist:
            return hist
        time.sleep(0.4)
    raise TimeoutError("Timed out waiting for ComfyUI output")

def comfy_view_image(filename: str, subfolder: str = "", type_: str = "output") -> bytes:
    r = requests.get(
        f"{COMFY_URL}/view",
        params={"filename": filename, "subfolder": subfolder, "type": type_},
        timeout=60,
    )
    r.raise_for_status()
    return r.content

def png_to_jpeg_b64(png_bytes: bytes, quality: int) -> str:
    im = Image.open(BytesIO(png_bytes)).convert("RGB")
    buf = BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("utf-8")

def handler(event):
    inp = event.get("input", {}) or {}
    image_url = inp.get("image_url")
    if not image_url:
        return {"error": "Missing required field: input.image_url"}

    # choose seed
    seed = int(get(inp, "seed"))
    if seed == -1:
        seed = random.randint(0, 2**31 - 1)

    prompt = get(inp, "prompt")
    negative = get(inp, "negative_prompt")

    # load workflow template from file in repo
    workflow = json.load(open("sdxl_simple_example.json", "r"))

    # upload reference image
    ref_bytes = fetch_bytes(image_url)
    ref_png = to_png_bytes(ref_bytes)
    uploaded = comfy_upload_image(ref_png, filename="ref.png")

    # patch reference image
    workflow["53"]["inputs"]["image"] = uploaded

    # patch prompts (base + refiner)
    workflow["6"]["inputs"]["text"] = prompt
    workflow["7"]["inputs"]["text"] = negative
    workflow["15"]["inputs"]["text"] = prompt
    workflow["16"]["inputs"]["text"] = negative

    # patch base sampler (node 56)
    workflow["56"]["inputs"]["seed"] = seed
    workflow["56"]["inputs"]["steps"] = int(get(inp, "steps_base"))
    workflow["56"]["inputs"]["cfg"] = float(get(inp, "cfg_base"))
    workflow["56"]["inputs"]["sampler_name"] = get(inp, "sampler_base")
    workflow["56"]["inputs"]["scheduler"] = get(inp, "scheduler_base")
    workflow["56"]["inputs"]["denoise"] = float(get(inp, "denoise"))

    # patch refiner sampler (node 11)
    workflow["11"]["inputs"]["noise_seed"] = seed
    workflow["11"]["inputs"]["steps"] = int(get(inp, "steps_refiner"))
    workflow["11"]["inputs"]["cfg"] = float(get(inp, "cfg_refiner"))
    workflow["11"]["inputs"]["sampler_name"] = get(inp, "sampler_refiner")
    workflow["11"]["inputs"]["scheduler"] = get(inp, "scheduler_refiner")
    workflow["11"]["inputs"]["start_at_step"] = int(get(inp, "refiner_start_at_step"))

    prompt_id = comfy_submit(workflow)
    hist = comfy_wait_history(prompt_id)

    # SaveImage node is "19"
    img_info = hist["outputs"]["19"]["images"][0]
    out_png = comfy_view_image(img_info["filename"], img_info.get("subfolder", ""), "output")

    jpeg_b64 = png_to_jpeg_b64(out_png, quality=int(get(inp, "jpeg_quality")))

    # Optional: upload to S3-compatible storage if configured
    image_url_out = None
    s3_put_url = os.environ.get("RESULT_PRESIGNED_PUT_URL")  # simplest: you provide a presigned PUT URL
    s3_get_url = os.environ.get("RESULT_PUBLIC_URL")         # and the matching public GET URL
    if s3_put_url and s3_get_url:
        pr = requests.put(s3_put_url, data=out_png, headers={"Content-Type": "image/png"}, timeout=60)
        pr.raise_for_status()
        image_url_out = s3_get_url

    return {
        "image_b64": jpeg_b64,
        "image_url": image_url_out,
        "seed": seed,
        "prompt_id": prompt_id
    }

if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
