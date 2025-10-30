import os
import tempfile
import shutil
import subprocess
import sys
import uuid
import logging
from pathlib import Path
from flask import Flask, request, send_file, jsonify, render_template, flash, redirect, url_for
from werkzeug.utils import secure_filename

# Import archive handling libraries with fallbacks
try:
    import magic
    MAGIC_SUPPORTED = True
except ImportError:
    MAGIC_SUPPORTED = False
    logging.warning("python-magic not available, falling back to extension-based detection")

try:
    import rarfile
    RAR_SUPPORTED = True
except ImportError:
    RAR_SUPPORTED = False
    logging.warning("rarfile not available, RAR support disabled")

try:
    import py7zr
    SEVENZ_SUPPORTED = True
except ImportError:
    SEVENZ_SUPPORTED = False
    logging.warning("py7zr not available, 7z support disabled")

import zipfile
import tarfile

# Import app from app.py to avoid circular import issues
from app import app

logger = logging.getLogger(__name__)

# Supported archive formats
SUPPORTED_EXTENSIONS = {'.zip', '.tar', '.tar.gz', '.tar.bz2', '.tar.xz', '.rar', '.7z'}
ARCHIVE_MIMES = {
    'application/zip': '.zip',
    'application/x-zip-compressed': '.zip',
    'application/x-tar': '.tar',
    'application/x-gtar': '.tar',
    'application/x-rar-compressed': '.rar',
    'application/x-7z-compressed': '.7z',
    'application/x-bzip2': '.tar.bz2',
    'application/x-xz': '.tar.xz',
}

def detect_file_type(file_path: Path) -> str:
    """Detect file type using magic numbers if available, fallback to extension."""
    if MAGIC_SUPPORTED:
        try:
            mime = magic.from_file(str(file_path), mime=True)
            logger.debug(f"Detected MIME type: {mime} for file: {file_path}")
            return mime
        except Exception as e:
            logger.warning(f"Magic detection failed: {e}, falling back to extension")
    
    # Fallback to extension-based detection
    suffix = file_path.suffix.lower()
    if suffix == '.zip':
        return 'application/zip'
    elif suffix in ['.tar', '.tar.gz', '.tar.bz2', '.tar.xz']:
        return 'application/x-tar'
    elif suffix == '.rar':
        return 'application/x-rar-compressed'
    elif suffix == '.7z':
        return 'application/x-7z-compressed'
    else:
        return 'application/octet-stream'

def is_archive_file(file_path: Path) -> bool:
    """Check if file is a supported archive format."""
    mime_type = detect_file_type(file_path)
    extension = file_path.suffix.lower()
    filename = file_path.name.lower()
    
    logger.debug(f"Checking archive format for {file_path}: mime={mime_type}, extension={extension}, filename={filename}")
    
    # Check by MIME type first
    if mime_type in ARCHIVE_MIMES:
        logger.debug(f"File recognized by MIME type: {mime_type}")
        return True
    
    # Fallback to extension check
    if extension in SUPPORTED_EXTENSIONS:
        logger.debug(f"File recognized by extension: {extension}")
        return True
    
    # Special handling for compressed tar files
    if filename.endswith(('.tar.gz', '.tar.bz2', '.tar.xz')):
        logger.debug(f"File recognized as compressed tar: {filename}")
        return True
    
    logger.warning(f"File not recognized as supported archive: {file_path} (mime: {mime_type}, ext: {extension})")
    return False

def compile_latex(tex_path: Path, output_path: Path, work_dir: Path) -> bool:
    """Compile LaTeX document to PDF with proper PythonTeX support."""
    main_tex_name = tex_path.name
    logger.info(f"Starting LaTeX compilation for {main_tex_name}")

    try:
        # First pass with latexmk - use -f flag to force processing even with missing files
        logger.debug("Running first latexmk pass (with -f for pythontex compatibility)")
        result = subprocess.run(
            [
                "latexmk",
                "-synctex=1",
                "-interaction=nonstopmode",
                "-file-line-error",
                "-pdf",
                "-f",  # Force processing even if there are errors
                "-outdir=" + str(work_dir),
                "--shell-escape",
                str(tex_path),
            ],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )
        
        # With -f flag, we check if pythontex files were generated rather than checking return code
        logger.debug(f"First latexmk pass completed with return code: {result.returncode}")

        # Check if pythontex is needed by looking for \usepackage{pythontex} or generated files
        needs_pythontex = False
        
        # Check for pythontex code files
        pythontex_files = list(work_dir.glob("*.pytxcode"))
        if pythontex_files:
            needs_pythontex = True
            logger.debug(f"Found {len(pythontex_files)} pythontex code files")
        
        # Also check the .tex file content
        try:
            with open(tex_path, 'r', encoding='utf-8', errors='ignore') as f:
                tex_content = f.read()
                if '\\usepackage{pythontex}' in tex_content or '\\usepackage[' in tex_content and 'pythontex' in tex_content:
                    needs_pythontex = True
                    logger.debug("Detected pythontex package in .tex file")
        except Exception as e:
            logger.warning(f"Could not read .tex file to check for pythontex: {e}")

        # Run pythontex if needed
        if needs_pythontex:
            logger.debug("Running pythontex")
            result = subprocess.run(
                [
                    "pythontex",
                    "--interpreter",
                    "python:" + sys.executable,
                    str(work_dir / main_tex_name),
                ],
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=300
            )
            
            if result.returncode != 0:
                logger.warning(f"pythontex failed: {result.stderr}")
                # Continue anyway - the final pass might still work

            # Final pass with latexmk after pythontex
            logger.debug("Running final latexmk pass")
            result = subprocess.run(
                [
                    "latexmk",
                    "-synctex=1",
                    "-interaction=nonstopmode",
                    "-file-line-error",
                    "-pdf",
                    "-outdir=" + str(work_dir),
                    "--shell-escape",
                    str(tex_path),
                ],
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=300
            )
            
            if result.returncode != 0:
                logger.error(f"Final latexmk pass failed: {result.stderr}")
                # Check if PDF was generated anyway
                if not output_path.exists():
                    return False
        else:
            # No pythontex needed, check if first pass failed
            if result.returncode != 0 and not output_path.exists():
                logger.error(f"Latexmk compilation failed: {result.stderr}")
                return False

        logger.info(f"LaTeX compilation completed. PDF exists: {output_path.exists()}")
        return output_path.exists()

    except subprocess.TimeoutExpired:
        logger.error("LaTeX compilation timed out")
        return False
    except subprocess.CalledProcessError as e:
        logger.error("LaTeX compilation failed")
        logger.error(f"Command: {e.cmd}")
        logger.error(f"Exit code: {e.returncode}")
        logger.error(f"Stdout: {e.stdout}")
        logger.error(f"Stderr: {e.stderr}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error during LaTeX compilation: {e}")
        return False

def save_uploaded_file(uploaded) -> Path:
    """Save werkzeug FileStorage object to a real temp file with correct suffix."""
    filename = secure_filename(uploaded.filename)
    
    # Handle compound extensions like .tar.gz, .tar.bz2, etc.
    if filename.endswith(('.tar.gz', '.tar.bz2', '.tar.xz')):
        # Extract the compound extension
        suffix = '.' + filename.split('.', 1)[1]
    else:
        suffix = Path(filename).suffix
    
    # Use tempfile.mkdtemp to create a directory and then create the file
    temp_dir = Path(tempfile.mkdtemp())
    tmp_file = temp_dir / f"upload{suffix}"
    
    logger.debug(f"Saving uploaded file '{filename}' with suffix '{suffix}' to {tmp_file}")
    
    try:
        # Read the entire content first to avoid stream issues
        content = uploaded.read()
        
        logger.debug(f"Read {len(content)} bytes from uploaded file")
        
        # Write to the temporary file
        with open(tmp_file, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        
        # Verify the file was written correctly
        if tmp_file.exists():
            file_size = tmp_file.stat().st_size
            logger.debug(f"Saved file size: {file_size} bytes")
            
            if file_size == 0:
                raise ValueError("Saved file is empty")
                
            # For ZIP files, do a quick validation
            if suffix.lower() == '.zip':
                logger.debug(tmp_file)
                try:
                    with zipfile.ZipFile(tmp_file, 'r') as test_zf:
                        test_zf.testzip()
                    logger.debug("ZIP file validation successful")
                except zipfile.BadZipFile as e:
                    logger.error(f"ZIP validation failed: {e}")
                    raise ValueError(f"Invalid ZIP file after save: {e}")
        else:
            raise ValueError("Failed to create temporary file")
            
    except Exception as e:
        # Clean up on error
        try:
            if tmp_file.exists():
                tmp_file.unlink()
            if temp_dir.exists():
                temp_dir.rmdir()
        except:
            pass
        raise e
    
    return tmp_file

def extract_archive(archive_path: Path, dest: Path):
    """Extract archive into destination folder with enhanced format support."""
    logger.info(f"Extracting archive {archive_path} to {dest}")
    
    if not is_archive_file(archive_path):
        raise ValueError(f"Unsupported archive format: {archive_path.suffix}")
    
    mime_type = detect_file_type(archive_path)
    suffix = archive_path.suffix.lower()
    
    try:
        # Verify file exists and is readable
        if not archive_path.exists():
            raise FileNotFoundError(f"Archive file not found: {archive_path}")
        
        # ZIP files
        if mime_type in ['application/zip', 'application/x-zip-compressed'] or suffix == '.zip':
            # Test if it's a valid ZIP file first
            try:
                with zipfile.ZipFile(archive_path, 'r') as zf:
                    zf.testzip()  # Test the ZIP file integrity
                    zf.extractall(dest)
                    logger.debug(f"Extracted ZIP archive with {len(zf.namelist())} files")
            except zipfile.BadZipFile as e:
                logger.error(f"Invalid ZIP file: {archive_path} - {e}")
                raise ValueError(f"Invalid ZIP file: {e}")
        
        # TAR files (including compressed variants)
        elif 'tar' in mime_type.lower() or suffix.startswith('.tar') or archive_path.name.endswith(('.tar.gz', '.tar.bz2', '.tar.xz')):
            with tarfile.open(archive_path, 'r:*') as tf:
                tf.extractall(dest)
                logger.debug(f"Extracted TAR archive with {len(tf.getnames())} files")
        
        # RAR files
        elif mime_type == 'application/x-rar-compressed' or suffix == '.rar':
            if not RAR_SUPPORTED:
                raise RuntimeError("RAR format not supported (rarfile module not available)")
            with rarfile.RarFile(str(archive_path)) as rf:
                rf.extractall(dest)
                logger.debug(f"Extracted RAR archive with {len(rf.namelist())} files")
        
        # 7Z files
        elif mime_type == 'application/x-7z-compressed' or suffix == '.7z':
            if not SEVENZ_SUPPORTED:
                raise RuntimeError("7Z format not supported (py7zr module not available)")
            with py7zr.SevenZipFile(archive_path, mode='r') as szf:
                szf.extractall(dest)
                logger.debug("Extracted 7Z archive")
        
        else:
            # Fallback to shutil for other formats
            logger.debug("Using shutil.unpack_archive as fallback")
            shutil.unpack_archive(str(archive_path), str(dest))
    
    except Exception as e:
        logger.error(f"Failed to extract archive {archive_path}: {e}")
        raise

@app.route("/compile", methods=["POST"])
def compile_latex_endpoint():
    """API endpoint for LaTeX compilation."""
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    uploaded = request.files["file"]
    
    if uploaded.filename == '':
        return jsonify({"error": "No file selected"}), 400

    # Create working directory
    workdir = Path(tempfile.mkdtemp())
    archive_path = None
    
    try:
        # Save and validate uploaded file
        try:
            archive_path = save_uploaded_file(uploaded)
            
            if not is_archive_file(archive_path):
                detected_type = detect_file_type(archive_path)
                return jsonify({
                    "error": f"Unsupported file format. File: {uploaded.filename}, Detected type: {detected_type}. Supported formats: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
                }), 400
            
            extract_archive(archive_path, workdir)
            
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            logger.error(f"Failed to process uploaded file: {e}")
            return jsonify({"error": f"Failed to extract archive: {str(e)}"}), 400

        # Look for main.tex file
        main_tex = workdir / "main.tex"
        if not main_tex.exists():
            # Try to find any .tex file as fallback
            tex_files = list(workdir.glob("**/*.tex"))
            if not tex_files:
                return jsonify({
                    "error": "No LaTeX files found. Please ensure your archive contains main.tex or other .tex files."
                }), 400
            main_tex = tex_files[0]
            logger.info(f"Using {main_tex.name} as main LaTeX file")

        # Compile LaTeX
        pdf_name = main_tex.stem + ".pdf"
        pdf_file = workdir / pdf_name
        
        logger.info(f"Starting compilation of {main_tex}")
        result = compile_latex(main_tex, pdf_file, workdir)

        if result and pdf_file.exists():
            logger.info("Compilation successful, returning PDF")
            return send_file(
                pdf_file, 
                mimetype="application/pdf",
                as_attachment=True,
                download_name=f"{pdf_name}"
            )
        else:
            # Look for log files to provide better error information
            log_files = list(workdir.glob("*.log"))
            error_info = "LaTeX compilation failed"
            
            if log_files:
                try:
                    with open(log_files[0], 'r', encoding='utf-8', errors='ignore') as f:
                        log_content = f.read()
                        # Extract relevant error information
                        if "! " in log_content:
                            error_lines = [line for line in log_content.split('\n') if line.startswith('! ')]
                            if error_lines:
                                error_info = f"LaTeX Error: {error_lines[0][2:]}"
                except Exception:
                    pass
            
            return jsonify({"error": error_info}), 400

    finally:
        # Cleanup - handle Windows permission issues
        if archive_path and archive_path.exists():
            try:
                # Wait a moment for file handles to be released
                import time
                time.sleep(0.1)
                
                # Clean up both the file and its parent temp directory
                temp_parent = archive_path.parent
                archive_path.unlink()
                
                # Try to remove the temporary directory if it's empty
                try:
                    if temp_parent.name.startswith('tmp') and not list(temp_parent.iterdir()):
                        temp_parent.rmdir()
                except:
                    pass
                    
            except PermissionError as e:
                logger.warning(f"Could not delete temporary archive file {archive_path}: {e}")
                # Try to delete later or let OS handle it
            except Exception as e:
                logger.warning(f"Unexpected error deleting archive file {archive_path}: {e}")
        
        if workdir.exists():
            shutil.rmtree(workdir, ignore_errors=True)

@app.route("/health")
def health_check():
    """Health check endpoint."""
    return jsonify({
        "status": "healthy",
        "supported_formats": list(SUPPORTED_EXTENSIONS),
        "features": {
            "magic_detection": MAGIC_SUPPORTED,
            "rar_support": RAR_SUPPORTED,
            "7z_support": SEVENZ_SUPPORTED
        }
    })
