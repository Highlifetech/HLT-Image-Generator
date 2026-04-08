import os
import uuid
import time
import tempfile
import base64
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, send_file, session, redirect, url_for
from PIL import Image
import io
import zipfile
import threading

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Configuration
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
APP_PASSWORD = os.environ.get('APP_PASSWORD', 'hlt2024')
PORT = int(os.environ.get('PORT', 8080))

# Temp directory for generated images
TEMP_DIR = os.path.join(tempfile.gettempdir(), 'hlt_images')
os.makedirs(TEMP_DIR, exist_ok=True)

# Gallery storage (in-memory, last 20 generations)
gallery = []
MAX_GALLERY = 20

# Initialize Gemini client
gemini_client = None
try:
    if GEMINI_API_KEY:
        from google import genai
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        print("Gemini API client initialized successfully")
    else:
        print("WARNING: GEMINI_API_KEY not set - image generation will not work")
except Exception as e:
    print(f"WARNING: Failed to initialize Gemini client: {e}")

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            return jsonify({'error': 'Not authenticated'}), 401
        return f(*args, **kwargs)
    return decorated

def cleanup_old_images():
    """Remove images older than 24 hours"""
    while True:
        try:
            cutoff = time.time() - 86400
            for f in os.listdir(TEMP_DIR):
                fp = os.path.join(TEMP_DIR, f)
                if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
        except Exception as e:
            print(f"Cleanup error: {e}")
        time.sleep(3600)

# Start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_old_images, daemon=True)
cleanup_thread.start()

def build_lifestyle_prompt(scene, model_type, custom_prompt=""):
    """Build a detailed prompt for lifestyle image generation"""
    scene_descriptions = {
        'street': 'urban street style setting, city sidewalk, natural daylight, modern architecture background',
        'studio': 'professional photography studio, clean backdrop, studio lighting, fashion editorial style',
        'outdoor': 'beautiful outdoor natural setting, golden hour lighting, scenic landscape background',
        'urban': 'trendy urban environment, graffiti walls, industrial chic, street fashion photography',
        'beach': 'sunny beach setting, ocean waves, sandy shore, tropical vibes, natural sunlight',
        'rooftop': 'stylish rooftop terrace, city skyline view, sunset lighting, upscale atmosphere',
        'coffee': 'cozy coffee shop interior, warm ambient lighting, lifestyle casual setting',
        'gym': 'modern fitness studio, athletic environment, dynamic lighting, active lifestyle'
    }
    
    model_descriptions = {
        'woman': 'a stylish young woman model',
        'man': 'a fashionable young man model',
        'diverse': 'a diverse group of models'
    }
    
    scene_desc = scene_descriptions.get(scene, custom_prompt or 'professional lifestyle setting')
    model_desc = model_descriptions.get(model_type, 'a professional model')
    
    if custom_prompt:
        scene_desc = custom_prompt
    
    prompt = (
        f"Professional e-commerce lifestyle photography of {model_desc} wearing or using "
        f"the product shown in the reference image. Setting: {scene_desc}. "
        f"High-quality, photorealistic, magazine-worthy fashion photography. "
        f"Natural poses, authentic lifestyle feel. 8K quality, professional lighting, "
        f"shot on Canon EOS R5, 85mm lens, shallow depth of field."
    )
    
    return prompt

def generate_with_gemini(prompt, product_image_path=None, num_images=4):
    """Generate images using Gemini API with Imagen model"""
    if not gemini_client:
        print("Gemini client not initialized")
        return None
    
    try:
        generated = []
        
        for i in range(min(num_images, 4)):
            try:
                # Use imagen-3.0-generate-002 for image generation
                response = gemini_client.models.generate_images(
                    model='imagen-3.0-generate-002',
                    prompt=prompt,
                    config={
                        'number_of_images': 1,
                        'aspect_ratio': '1:1',
                        'safety_filter_level': 'BLOCK_MEDIUM_AND_ABOVE',
                    }
                )
                
                if response and response.generated_images:
                    for img_data in response.generated_images:
                        filename = f"{uuid.uuid4().hex}_{i}.png"
                        filepath = os.path.join(TEMP_DIR, filename)
                        
                        # Save image from response
                        img_bytes = img_data.image.image_bytes
                        with open(filepath, 'wb') as f:
                            f.write(img_bytes)
                        
                        generated.append(filename)
                        
            except Exception as img_err:
                print(f"Image generation attempt {i} failed: {img_err}")
                continue
        
        return generated if generated else None
        
    except Exception as e:
        print(f"Gemini generation failed: {e}")
        return None

@app.route('/')
def index():
    """Serve the main page"""
    return send_file('index.html')

@app.route('/login', methods=['POST'])
def login():
    """Handle team authentication"""
    data = request.get_json()
    password = data.get('password', '')
    
    if password == APP_PASSWORD:
        session['authenticated'] = True
        return jsonify({'success': True})
    return jsonify({'error': 'Invalid password'}), 401

@app.route('/logout')
def logout():
    session.pop('authenticated', None)
    return redirect('/')

@app.route('/generate', methods=['POST'])
@require_auth
def generate():
    """Generate lifestyle images from uploaded product photo"""
    if not gemini_client:
        return jsonify({'error': 'Image generation service not configured. Please set GEMINI_API_KEY.'}), 500
    
    # Get uploaded file
    if 'product_image' not in request.files:
        return jsonify({'error': 'No product image uploaded'}), 400
    
    file = request.files['product_image']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    # Get generation parameters
    scene = request.form.get('scene', 'studio')
    model_type = request.form.get('model_type', 'woman')
    custom_prompt = request.form.get('custom_prompt', '')
    num_images = min(int(request.form.get('num_images', 4)), 4)
    
    # Save uploaded file temporarily
    upload_filename = f"upload_{uuid.uuid4().hex}.png"
    upload_path = os.path.join(TEMP_DIR, upload_filename)
    
    try:
        img = Image.open(file.stream)
        img.save(upload_path, 'PNG')
    except Exception as e:
        return jsonify({'error': f'Invalid image file: {str(e)}'}), 400
    
    # Build prompt
    prompt = build_lifestyle_prompt(scene, model_type, custom_prompt)
    
    # Generate images
    generated_files = generate_with_gemini(prompt, upload_path, num_images)
    
    if not generated_files:
        return jsonify({'error': 'Image generation failed. Please try again.'}), 500
    
    # Build response with image URLs
    images = []
    for filename in generated_files:
        images.append({
            'url': f'/images/{filename}',
            'filename': filename
        })
    
    # Add to gallery
    gallery_entry = {
        'id': uuid.uuid4().hex,
        'timestamp': datetime.now().isoformat(),
        'scene': scene,
        'model_type': model_type,
        'original': upload_filename,
        'generated': generated_files,
        'prompt': prompt
    }
    gallery.insert(0, gallery_entry)
    if len(gallery) > MAX_GALLERY:
        gallery.pop()
    
    return jsonify({
        'success': True,
        'images': images,
        'original': f'/images/{upload_filename}',
        'prompt': prompt
    })

@app.route('/images/<filename>')
def serve_image(filename):
    """Serve generated images"""
    filepath = os.path.join(TEMP_DIR, filename)
    if os.path.exists(filepath):
        return send_file(filepath, mimetype='image/png')
    return jsonify({'error': 'Image not found'}), 404

@app.route('/download/<filename>')
@require_auth
def download(filename):
    """Download a single image"""
    filepath = os.path.join(TEMP_DIR, filename)
    if os.path.exists(filepath):
        return send_file(filepath, as_attachment=True, download_name=filename)
    return jsonify({'error': 'Image not found'}), 404

@app.route('/download_all', methods=['POST'])
@require_auth
def download_all():
    """Download all generated images as a zip"""
    data = request.get_json()
    filenames = data.get('filenames', [])
    
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
        download_name=f'hlt_lifestyle_images_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip'
    )

@app.route('/gallery')
@require_auth
def get_gallery():
    """Get recent generations"""
    return jsonify({'gallery': gallery})

@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'gemini_configured': gemini_client is not None,
        'timestamp': datetime.now().isoformat()
    })

if __name__ == '__main__':
    print(f"Starting HLT Image Generator on port {PORT}")
    print(f"Gemini API configured: {gemini_client is not None}")
    app.run(host='0.0.0.0', port=PORT, debug=False)
