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
import requests

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
        creds_data = json.loads(creds_json)
        credentials = service_account.Credentials.from_service_account_info(creds_data)
        print("Google Cloud credentials loaded successfully")
    except Exception as e:
        print(f"Warning: Could not load credentials: {e}")

# Initialize Vertex AI
imagen_model = None
try:
    if credentials and PROJECT_ID:
        import vertexai
        from vertexai.preview.vision_models import ImageGenerationModel
        vertexai.init(project=PROJECT_ID, location=LOCATION, credentials=credentials)
        imagen_model = ImageGenerationModel.from_pretrained("imagen-3.0-generate-001")
        print("Vertex AI Imagen model initialized successfully")
    else:
        print("Warning: Missing credentials or project ID - Imagen model not initialized")
except Exception as e:
    print(f"Warning: Could not initialize Imagen model: {e}")

# Initialize Cloud Storage
storage_client = None
try:
    if credentials:
        storage_client = storage.Client(credentials=credentials, project=PROJECT_ID)
        print("Cloud Storage client initialized successfully")
    else:
        print("Warning: No credentials - Storage client not initialized")
except Exception as e:
    print(f"Warning: Could not initialize Storage client: {e}")

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('authenticated'):
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

def cleanup_old_files():
    """Remove files older than 24 hours"""
    while True:
        try:
            now = time.time()
            cutoff = now - 86400
            for filename in os.listdir(TEMP_DIR):
                filepath = os.path.join(TEMP_DIR, filename)
                if os.path.isfile(filepath) and os.path.getmtime(filepath) < cutoff:
                    os.remove(filepath)
                    print(f"Cleaned up old file: {filename}")
        except Exception as e:
            print(f"Cleanup error: {e}")
        time.sleep(3600)

# Start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()

def build_lifestyle_prompt(scene, model_type, product_description="clothing item"):
    """Build a detailed prompt for lifestyle image generation"""
    scene_descriptions = {
        'street': 'urban street style setting, city sidewalk, modern architecture background, natural daylight',
        'studio': 'professional photography studio, clean backdrop, studio lighting, fashion editorial style',
        'outdoor': 'outdoor natural setting, park or garden, golden hour lighting, lifestyle photography',
        'urban': 'trendy urban environment, graffiti walls, industrial chic, street fashion vibes',
        'beach': 'beach setting, ocean waves, sandy shore, tropical vibes, summer lifestyle',
        'rooftop': 'rooftop setting, city skyline view, sunset lighting, elevated lifestyle',
        'coffee': 'cozy coffee shop interior, warm ambient lighting, casual lifestyle, cafe setting',
        'gym': 'modern gym or fitness studio, athletic lifestyle, energetic mood, workout setting'
    }
    
    model_descriptions = {
        'woman': 'a stylish young woman',
        'man': 'a fashionable young man',
        'diverse': 'a diverse group of models'
    }
    
    scene_desc = scene_descriptions.get(scene, scene)
    model_desc = model_descriptions.get(model_type, 'a fashion model')
    
    prompt = f"Professional lifestyle fashion photography of {model_desc} wearing {product_description}, "
    prompt += f"{scene_desc}. High quality, editorial style, realistic, 8k resolution, "
    prompt += "professional color grading, sharp focus, natural pose, authentic lifestyle moment."
    
    return prompt

def try_virtual_tryon(image_data, model_type):
    """Attempt Virtual Try-On API"""
    if not credentials or not PROJECT_ID:
        return None
    try:
        auth_req = requests.Request()
        credentials.refresh(auth_req)
        endpoint = f"https://{LOCATION}-aiplatform.googleapis.com/v1/projects/{PROJECT_ID}/locations/{LOCATION}/publishers/google/models/virtual-try-on-001:predict"
        
        img_b64 = base64.b64encode(image_data).decode('utf-8')
        
        payload = {
            "instances": [{
                "image": {"bytesBase64Encoded": img_b64},
                "modelType": model_type
            }],
            "parameters": {"sampleCount": 1}
        }
        
        headers = {
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json"
        }
        
        response = requests.post(endpoint, json=payload, headers=headers, timeout=60)
        if response.status_code == 200:
            result = response.json()
            if 'predictions' in result:
                return result['predictions']
        print(f"Virtual Try-On returned status {response.status_code}")
        return None
    except Exception as e:
        print(f"Virtual Try-On failed: {e}")
        return None

def generate_with_imagen(prompt, num_images=4):
    """Generate images using Imagen model"""
    if not imagen_model:
        return None
    try:
        response = imagen_model.generate_images(
            prompt=prompt,
            number_of_images=min(num_images, 4),
            aspect_ratio="1:1"
        )
        
        generated = []
        for idx, img in enumerate(response.images):
            filename = f"{uuid.uuid4().hex}_{idx}.png"
            filepath = os.path.join(TEMP_DIR, filename)
            img.save(filepath)
            generated.append(filename)
        
        return generated
    except Exception as e:
        print(f"Imagen generation failed: {e}")
        return None

@app.route('/')
def index():
    """Serve the main page"""
    return send_file('index.html')

@app.route('/login', methods=['POST'])
def login():
    """Handle login"""
    data = request.get_json()
    password = data.get('password', '')
    
    if password == APP_PASSWORD:
        session['authenticated'] = True
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Invalid password'}), 401

@app.route('/logout')
def logout():
    """Handle logout"""
    session.pop('authenticated', None)
    return redirect(url_for('index'))

@app.route('/generate', methods=['POST'])
@login_required
def generate():
    """Generate lifestyle images"""
    if not imagen_model:
        return jsonify({'error': 'Image generation service not configured. Please check your Google Cloud credentials.'}), 503
    
    if 'image' not in request.files:
        return jsonify({'error': 'No image uploaded'}), 400
    
    file = request.files['image']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    scene = request.form.get('scene', 'studio')
    model_type = request.form.get('model', 'woman')
    num_images = int(request.form.get('num_images', 4))
    custom_scene = request.form.get('custom_scene', '')
    
    try:
        image_data = file.read()
        
        # Save original upload
        original_filename = f"original_{uuid.uuid4().hex}.png"
        original_path = os.path.join(TEMP_DIR, original_filename)
        with open(original_path, 'wb') as f:
            f.write(image_data)
        
        # Upload to GCS if available
        if storage_client and GCS_BUCKET:
            try:
                bucket = storage_client.bucket(GCS_BUCKET)
                blob = bucket.blob(f"uploads/{original_filename}")
                blob.upload_from_string(image_data, content_type='image/png')
            except Exception as e:
                print(f"GCS upload failed: {e}")
        
        generated_files = []
        
        # Try Virtual Try-On first
        tryon_results = try_virtual_tryon(image_data, model_type)
        if tryon_results:
            for idx, pred in enumerate(tryon_results):
                if 'bytesBase64Encoded' in pred:
                    img_bytes = base64.b64decode(pred['bytesBase64Encoded'])
                    filename = f"{uuid.uuid4().hex}_{idx}.png"
                    filepath = os.path.join(TEMP_DIR, filename)
                    with open(filepath, 'wb') as f:
                        f.write(img_bytes)
                    generated_files.append(filename)
        
        # Fall back to Imagen text-to-image
        if not generated_files:
            scene_text = custom_scene if custom_scene else scene
            prompt = build_lifestyle_prompt(scene_text, model_type)
            generated_files = generate_with_imagen(prompt, num_images)
        
        if not generated_files:
            return jsonify({'error': 'Failed to generate images. Please try again.'}), 500
        
        # Add to gallery
        gallery_entry = {
            'id': uuid.uuid4().hex,
            'timestamp': datetime.now().isoformat(),
            'original': original_filename,
            'generated': generated_files,
            'scene': scene,
            'model': model_type
        }
        gallery.insert(0, gallery_entry)
        if len(gallery) > MAX_GALLERY:
            gallery.pop()
        
        return jsonify({
            'success': True,
            'original': original_filename,
            'generated': generated_files,
            'count': len(generated_files)
        })
        
    except Exception as e:
        print(f"Generation error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/gallery')
@login_required
def get_gallery():
    """Return gallery entries"""
    return jsonify({'gallery': gallery})

@app.route('/download/<filename>')
@login_required
def download(filename):
    """Download a single image"""
    filepath = os.path.join(TEMP_DIR, filename)
    if os.path.exists(filepath):
        return send_file(filepath, as_attachment=True, download_name=filename)
    return jsonify({'error': 'File not found'}), 404

@app.route('/download-all', methods=['POST'])
@login_required
def download_all():
    """Download all generated images as zip"""
    data = request.get_json()
    filenames = data.get('files', [])
    
    if not filenames:
        return jsonify({'error': 'No files specified'}), 400
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for filename in filenames:
            filepath = os.path.join(TEMP_DIR, filename)
            if os.path.exists(filepath):
                zf.write(filepath, filename)
    
    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'hlt_lifestyle_images_{uuid.uuid4().hex[:8]}.zip'
    )

@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'vertex_ai': imagen_model is not None,
        'storage': storage_client is not None,
        'project': PROJECT_ID,
        'location': LOCATION
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
