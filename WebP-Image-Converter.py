import os
import sys
from pathlib import Path
from PIL import Image
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Constants
WEBP_QUALITY = "100"
DELETE_ORIGINALS = True
SUPPORTED_FORMATS = {'.jpg', '.jpeg', '.png'}
MAX_WORKERS = os.cpu_count() or 1

def safe_decode(byte_string):
    """Safely decode a byte string, falling back to a simple representation if it fails."""
    try:
        return byte_string.decode('utf-8')
    except UnicodeDecodeError:
        return str(byte_string)

def run_subprocess(command):
    """Run a subprocess command and safely handle its output."""
    try:
        result = subprocess.run(command, capture_output=True, check=True)
        return safe_decode(result.stdout), safe_decode(result.stderr)
    except subprocess.CalledProcessError as e:
        logging.error(f"Command failed: {' '.join(command)}")
        logging.error(f"Error output: {safe_decode(e.stderr)}")
        raise

def process_image_file(file_path):
    try:
        if not any(file_path.lower().endswith(ext) for ext in SUPPORTED_FORMATS):
            logging.warning(f"Unsupported file format: {file_path}")
            return

        logging.info(f"Processing: {file_path}")
        
        filename = file_path
        filename_out = f'{os.path.splitext(filename)[0]}.webp'

        # Convert to WEBP
        stdout, stderr = run_subprocess(['cwebp', '-q', WEBP_QUALITY, filename, '-o', filename_out])
        logging.debug(f"cwebp stdout: {stdout}")
        logging.debug(f"cwebp stderr: {stderr}")

        if filename.lower().endswith('.png'):
            # Copy PNG Chunk data from original PNG
            with Image.open(filename) as im:
                user_comment = im.info.get("parameters", "")
            
            # Write EXIF to WEBP
            stdout, stderr = run_subprocess(['exiftool', '-overwrite_original', f'-UserComment={user_comment}', filename_out])
            logging.debug(f"exiftool stdout: {stdout}")
            logging.debug(f"exiftool stderr: {stderr}")

        if DELETE_ORIGINALS:
            os.remove(filename)
            logging.info(f"Deleted original file: {filename}")

        logging.info(f"Successfully processed: {file_path}")
    except subprocess.CalledProcessError:
        # Error already logged in run_subprocess
        pass
    except Exception as e:
        logging.error(f"Unexpected error processing {file_path}: {str(e)}")

def main():
    try:
        folder_path = input("Enter the folder path containing images: ").strip("\"'")
        folder_path = os.path.normpath(folder_path)
        
        if not os.path.exists(folder_path):
            logging.error("Folder not found!")
            return

        image_files = [
            os.path.join(root, file)
            for root, _, files in os.walk(folder_path)
            for file in files
            if any(file.lower().endswith(ext) for ext in SUPPORTED_FORMATS)
        ]

        total_files = len(image_files)
        logging.info(f"Found {total_files} image files to process.")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(process_image_file, file_path) for file_path in image_files]
            
            for i, future in enumerate(as_completed(futures), 1):
                try:
                    future.result()  # This will raise any exceptions that occurred
                except Exception as e:
                    logging.error(f"Error processing file {i}: {str(e)}")
                logging.info(f"Progress: {i}/{total_files} files processed")

        logging.info("All files processed!")

    except KeyboardInterrupt:
        logging.info("Process interrupted by user. Exiting...")
    except Exception as e:
        logging.error(f"An unexpected error occurred: {str(e)}")

if __name__ == "__main__":
    main()