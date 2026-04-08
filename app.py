import os
import json
import base64
import uuid
import time
import shutil
import tempfile
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, send_file, session, redirect, url_for
from google.cloud import storage
from google.oauth2 import service_account
from PIL import Image
import io
import zipfile
import threading

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Configuration
PROJECT_ID = os.environ.get('GOOGLE_CLOUD_PROJECT', '')
LOCATION = os.environ.get('GOOGLE_CLOUD_LOCATION', 'us-central1')
GCS_BUCKET = os.environ.get('GCS_BUCKET_NAME', '')
APP_PASSWORD = os.environ.get('APP_PASSWORD', 'hlt2024')
PORT = int(os.environ.get('PORT', 8080))

# Temp directory for generated images
TEMP_DIR = os.path.join(tempfile.gettempdir(), 'hlt_images')
os.makedirs(TEMP_DIR, exist_ok=True)

# Gallery storage (in-memory, last 20 generations)
gallery = []
MAX_GALLERY = 20

# Initialize credentials
credentials = None
creds_json = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS_JSON', '')
if creds_json:
      try:
                creds_info = json.loads(creds_json)
                credentials = service_account.Credentials.from_service_account_info(creds_info)
except Exception as e:
        print(f"Error loading credentials: {e}")

# Initialize Vertex AI
try:
      import vertexai
      from vertexai.preview.vision_models import ImageGenerationModel
      if credentials:
                vertexai.init(project=PROJECT_ID, location=LOCATION, credentials=credentials)
else:
          vertexai.init(project=PROJECT_ID, location=LOCATION)
      imagen_model = ImageGenerationModel.from_pretrained("imagen-3.0-generate-001")
    print("Vertex AI initialized successfully")
except Exception as e:
    imagen_model = None
    print(f"Error initializing Vertex AI: {e}")

# Initialize Cloud Storage
storage_client = None
try:
      if credentials:
                storage_client = storage.Client(project=PROJECT_ID, credentials=credentials)
else:
          storage_client = storage.Client(project=PROJECT_ID)
except Exception as e:
    print(f"Error initializing Cloud Storage: {e}")


def login_required(f):
      @wraps(f)
      def decorated_function(*args, **kwargs):
                if not session.get('authenticated'):
                              return jsonify({'error': 'Authentication required'}), 401
                          return f(*args, **kwargs)
            return decorated_function


def cleanup_old_files():
      """Remove files older than 24 hours"""
    try:
              cutoff = time.time() - 86400
              for filename in os.listdir(TEMP_DIR):
                            filepath = os.path.join(TEMP_DIR, filename)
                            if os.path.isfile(filepath) and os.path.getmtime(filepath) < cutoff:
                                              os.remove(filepath)
    except Exception as e:
        print(f"Cleanup error: {e}")


def schedule_cleanup():
      """Run cleanup every hour"""
    cleanup_old_files()
    timer = threading.Timer(3600, schedule_cleanup)
    timer.daemon = True
    timer.start()


schedule_cleanup()

SCENE_PROMPTS = {
      'street_style': 'professional fashion photography, model wearing the garment walking on a trendy urban street, natural lighting, editorial style, high fashion, street style photography',
      'studio': 'professional studio fashion photography, model wearing the garment, clean white background, soft studio lighting, high-end editorial look, commercial photography',
      'outdoor': 'professional outdoor fashion photography, model wearing the garment in a beautiful natural setting, golden hour lighting, lifestyle photography, magazine quality',
      'urban': 'professional urban fashion photography, model wearing the garment in a modern city environment, architectural background, contemporary style, editorial photography',
      'beach': 'professional beach fashion photography, model wearing the garment on a beautiful sandy beach, ocean in background, warm natural lighting, resort lifestyle photography',
      'rooftop': 'professional rooftop fashion photography, model wearing the garment on a stylish rooftop terrace, city skyline in background, golden hour, lifestyle editorial',
      'coffee_shop': 'professional lifestyle photography, model wearing the garment in a trendy coffee shop, warm ambient lighting, casual chic atmosphere, lifestyle editorial',
      'gym': 'professional athletic photography, model wearing the garment in a modern gym setting, dynamic pose, fitness lifestyle, activewear photography',
}

MODEL_PROMPTS = {
      'woman': 'beautiful diverse woman model',
      'man': 'handsome diverse male model',
      'diverse': 'diverse group of models',
}


def upload_to_gcs(image_bytes, filename):
      """Upload image bytes to Google Cloud Storage"""
    if not storage_client or not GCS_BUCKET:
              return None
          try:
                    bucket = storage_client.bucket(GCS_BUCKET)
                    blob = bucket.blob(f"uploads/{filename}")
                    blob.upload_from_string(image_bytes, content_type='image/png')
                    return f"gs://{GCS_BUCKET}/uploads/{filename}"
except Exception as e:
        print(f"GCS upload error: {e}")
        return None


def generate_with_imagen(prompt, num_images=4):
      """Generate images using Vertex AI Imagen"""
    if not imagen_model:
              raise Exception("Imagen model not initialized. Check your Google Cloud credentials.")

    try:
              response = imagen_model.generate_images(
                            prompt=prompt,
                            number_of_images=min(num_images, 4),
                            aspect_ratio="1:1",
              )

        generated = []
        for i, image in enumerate(response.images):
                      img_id = str(uuid.uuid4())[:8]
                      filename = f"generated_{img_id}.png"
                      filepath = os.path.join(TEMP_DIR, filename)
                      image.save(filepath)
                      generated.append({
                          'id': img_id,
                          'filename': filename,
                          'filepath': filepath,
                      })

        return generated
except Exception as e:
        raise Exception(f"Image generation failed: {str(e)}")


def try_virtual_tryon(garment_image_b64, model_type='woman', scene='studio'):
      """Attempt Virtual Try-On API (REST endpoint)"""
    import requests
    import google.auth.transport.requests

    try:
              endpoint = f"https://{LOCATION}-aiplatform.googleapis.com/v1/projects/{PROJECT_ID}/locations/{LOCATION}/publishers/google/models/virtual-try-on-001:predict"

        auth_req = google.auth.transport.requests.Request()
        credentials.refresh(auth_req)

        headers = {
                      'Authorization': f'Bearer {credentials.token}',
                      'Content-Type': 'application/json',
        }

        payload = {
                      'instances': [{
                                        'garment_image': {'bytesBase64Encoded': garment_image_b64},
                                        'model_type': model_type,
                      }],
                      'parameters': {
                                        'sampleCount': 2,
                      }
        }

        resp = requests.post(endpoint, headers=headers, json=payload, timeout=120)

        if resp.status_code == 200:
                      result = resp.json()
                      generated = []
                      for i, pred in enumerate(result.get('predictions', [])):
                                        if 'bytesBase64Encoded' in pred:
                                                              img_data = base64.b64decode(pred['bytesBase64Encoded'])
                                                              img_id = str(uuid.uuid4())[:8]
                                                              filename = f"tryon_{img_id}.png"
                                                              filepath = os.path.join(TEMP_DIR, filename)
                                                              with open(filepath, 'wb') as f:
                                                                                        f.write(img_data)
                                                                                    generated.append({
                                                                  'id': img_id,
                                                                  'filename': filename,
                                                                  'filepath': filepath,
                                                                                    })
                                                      return generated if generated else None
        else:
            print(f"Virtual Try-On API returned {resp.status_code}: {resp.text}")
                      return None
except Exception as e:
        print(f"Virtual Try-On error: {e}")
        return None


@app.route('/')
def index():
      """Serve the main HTML page"""
    return send_file('index.html')


@app.route('/login', methods=['POST'])
def login():
      """Handle login"""
    data = request.get_json()
    password = data.get('password', '')

    if password == APP_PASSWORD:
              session['authenticated'] = True
              return jsonify({'success': True})
else:
        return jsonify({'error': 'Invalid password'}), 401


@app.route('/logout', methods=['POST'])
def logout():
      """Handle logout"""
    session.pop('authenticated', None)
    return jsonify({'success': True})


@app.route('/generate', methods=['POST'])
@login_required
def generate():
      """Generate lifestyle images from uploaded product photo"""
    try:
              if 'image' not in request.files:
                            return jsonify({'error': 'No image uploaded'}), 400

              file = request.files['image']
              if file.filename == '':
                            return jsonify({'error': 'No file selected'}), 400

              scene = request.form.get('scene', 'studio')
              model_type = request.form.get('model', 'woman')
              custom_prompt = request.form.get('custom_prompt', '')
              num_images = int(request.form.get('num_images', 4))

        # Read and save uploaded image
              image_bytes = file.read()
        upload_id = str(uuid.uuid4())[:8]
        original_filename = f"original_{upload_id}.png"
        original_path = os.path.join(TEMP_DIR, original_filename)

        # Convert to PNG and save
        img = Image.open(io.BytesIO(image_bytes))
        img.save(original_path, 'PNG')

        # Upload to GCS
        upload_to_gcs(image_bytes, original_filename)

        # Convert to base64 for API calls
        buffered = io.BytesIO()
        img.save(buffered, format="PNG")
        img_b64 = base64.b64encode(buffered.getvalue()).decode('utf-8')

        generated_images = None
        method_used = 'none'

        # Priority 1: Try Virtual Try-On API
        if scene in ['studio', 'street_style'] and model_type != 'diverse':
                      try:
                                        vton_results = try_virtual_tryon(img_b64, model_type, scene)
                                        if vton_results:
                                                              generated_images = vton_results
                                                              method_used = 'virtual_tryon'
                      except Exception as e:
                                        print(f"Virtual Try-On fallback: {e}")

                  # Priority 2: Imagen with detailed prompt
                  if not generated_images:
                                model_desc = MODEL_PROMPTS.get(model_type, MODEL_PROMPTS['woman'])

            if custom_prompt:
                              scene_desc = custom_prompt
else:
                scene_desc = SCENE_PROMPTS.get(scene, SCENE_PROMPTS['studio'])

            prompt = f"{model_desc}, {scene_desc}, wearing a fashionable clothing item, photorealistic, high resolution, professional e-commerce lifestyle photography, 8k quality"

            try:
                              generated_images = generate_with_imagen(prompt, num_images)
                              method_used = 'imagen'
except Exception as e:
                return jsonify({'error': f'Generation failed: {str(e)}'}), 500

        if not generated_images:
                      return jsonify({'error': 'Failed to generate images'}), 500

        # Build response
        results = []
        for img_info in generated_images:
                      results.append({
                                        'id': img_info['id'],
                                        'filename': img_info['filename'],
                                        'url': f"/download/{img_info['filename']}",
                      })

        # Add to gallery
        gallery_entry = {
                      'id': upload_id,
                      'timestamp': datetime.now().isoformat(),
                      'original': original_filename,
                      'original_url': f"/download/{original_filename}",
                      'scene': scene,
                      'model': model_type,
                      'method': method_used,
                      'images': results,
        }
        gallery.insert(0, gallery_entry)
        if len(gallery) > MAX_GALLERY:
                      gallery.pop()

        return jsonify({
                      'success': True,
                      'method': method_used,
                      'original_url': f"/download/{original_filename}",
                      'images': results,
        })

except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/gallery')
@login_required
def get_gallery():
      """Return gallery of recent generations"""
    return jsonify({'gallery': gallery})


@app.route('/download/<filename>')
def download_file(filename):
      """Download a generated image"""
    # Sanitize filename
    filename = os.path.basename(filename)
    filepath = os.path.join(TEMP_DIR, filename)

    if os.path.exists(filepath):
              return send_file(filepath, mimetype='image/png', as_attachment=False)
else:
        return jsonify({'error': 'File not found'}), 404


@app.route('/download-all', methods=['POST'])
@login_required
def download_all():
      """Download all generated images as a zip"""
    try:
              data = request.get_json()
              filenames = data.get('filenames', [])

        if not filenames:
                      return jsonify({'error': 'No files specified'}), 400

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                      for fname in filenames:
                                        fname = os.path.basename(fname)
                                        filepath = os.path.join(TEMP_DIR, fname)
                                        if os.path.exists(filepath):
                                                              zip_file.write(filepath, fname)

                                zip_buffer.seek(0)
        return send_file(
                      zip_buffer,
                      mimetype='application/zip',
                      as_attachment=True,
                      download_name=f'hlt_lifestyle_images_{int(time.time())}.zip'
        )
except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/health')
def health():
      """Health check endpoint"""
    return jsonify({
              'status': 'healthy',
              'vertex_ai': imagen_model is not None,
              'storage': storage_client is not None,
              'project': PROJECT_ID,
              'location': LOCATION,
              'timestamp': datetime.now().isoformat(),
    })


if __name__ == '__main__':
      app.run(host='0.0.0.0', port=PORT, debug=False)
