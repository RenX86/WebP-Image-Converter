# WebP Image Converter

This Python script automates the process of converting JPG, JPEG, and PNG images to the WebP format. It's designed to process images in bulk, making it ideal for optimizing large image collections for web use.

## Features

- Converts JPG, JPEG, and PNG images to WebP format
- Maintains image quality with configurable compression settings
- Preserves PNG metadata in the converted WebP files
- Supports recursive directory scanning
- Provides detailed logging for easy troubleshooting
- Utilizes multi-threading for improved performance
- Includes a batch script for easy execution from anywhere on Windows systems

## Prerequisites

- Python 3.6 or higher
- Pillow library
- WebP command-line tools (`cwebp`)
- ExifTool

## Installation

1. Clone this repository or download the script files:
   ```
   git clone https://github.com/RenX86/WebP-Image-Converter.git
   ```
   or download `WebP-Image-Converter.py` and `run-webp-converter.bat` directly.

2. Install the required Python libraries:
   ```
   pip install pillow
   ```

3. Install WebP command-line tools:
   - For Windows: Download from [Google's WebP page](https://developers.google.com/speed/webp/download) and add to your PATH.
   - For macOS: Use Homebrew: `brew install webp`
   - For Linux: Use your distribution's package manager, e.g., `sudo apt-get install webp`

4. Install ExifTool: 
   - For Windows: Download from [ExifTool website](https://exiftool.org/) and add to your PATH.
      - In windows rename the 'exiftool(-k).exe' to 'exiftool.exe' after extraction.
      - Add the whole extrated folder to PATH in environment variables.
   - For macOS: Use Homebrew: `brew install exiftool`
   - For Linux: Use your distribution's package manager, e.g., `sudo apt-get install libimage-exiftool-perl`

5. (Optional for Windows users) Add the script directory to your system PATH to run it from anywhere:
   - Right-click on "This PC" or "My Computer" and select "Properties"
   - Click on "Advanced system settings"
   - Click on "Environment Variables"
   - Under "System variables", find and select the "Path" variable, then click "Edit"
   - Click "New" and add the full path to the directory containing your scripts
   - Click "OK" to close all dialogs

## Usage

### On Windows:

1. Open a command prompt.
2. Navigate to the script directory or run from anywhere if you've added it to your PATH.
3. Run the batch file:
   ```
   run-webp-converter
   ```
4. Enter the path to the folder containing your images when prompted.

### On macOS and Linux:

1. Open a terminal.
2. Navigate to the script directory.
3. Run the Python script directly:
   ```
   python WebP-Image-Converter.py
   ```
4. Enter the path to the folder containing your images when prompted.

## Configuration

You can modify the following variables at the top of `WebP-Image-Converter.py` to customize the script's behavior:

- `WEBP_QUALITY`: Set the quality of the WebP conversion (0-100)
- `DELETE_ORIGINALS`: Set to `True` to delete original files after conversion, `False` to keep them
- `MAX_WORKERS`: Set the maximum number of concurrent threads for processing

## Logging

The script provides detailed logging information. Check the console output for progress updates and any error messages.

## Contributing

Contributions to improve the script are welcome. Please feel free to submit a Pull Request.

## Acknowledgments

- This script uses the WebP tools developed by Google
- ExifTool by Phil Harvey is used for metadata handling
