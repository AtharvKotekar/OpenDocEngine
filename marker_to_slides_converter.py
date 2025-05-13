import json
import uuid
import os
from datetime import datetime, timezone
from bs4 import BeautifulSoup
import argparse
import subprocess
import tempfile # For temporary directory and files
import shutil   # For deleting directory tree
import base64   # For encoding/decoding images
import sys      # For stderr
import time

MY_ENV = os.environ.copy()

# --- Global Variable for Temporary Image Directory ---
TEMP_IMAGE_BASE_DIR = None # Will be initialized in main()

# --- Configuration & Mapping ---
IGNORED_BLOCK_TYPES = {"PageFooter", "PageHeader", "Footnote", "Form", "Handwriting", "TableOfContents"}
VISUAL_IMAGE_BLOCK_TYPES = {"Figure", "Picture", "FigureGroup", "PictureGroup"}
TEXTUAL_STRUCTURAL_BLOCK_TYPES = {"Table", "TableGroup", "Code", "Equation"}

MAX_TEXT_ELEMENTS_PER_SLIDE = 3
MAX_IMAGES_PER_SLIDE = 2
MAX_TEXT_WITH_IMAGES = 1

# --- Helper Functions ---
def get_clean_text(html_content):
    if not html_content:
        return ""
    soup = BeautifulSoup(html_content, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    return soup.get_text(separator=" ", strip=True).replace("\n ", "\n").strip()

def get_block_semantic_type(marker_block):
    block_type = marker_block.get("block_type")
    section_hierarchy = marker_block.get("section_hierarchy")

    if block_type == "SectionHeader":
        if section_hierarchy and isinstance(section_hierarchy, dict) and section_hierarchy:
            level = sorted(section_hierarchy.keys())[0]
            if level == "1": return "heading"
            elif level == "2": return "heading"
            else: return "subheading"
        return "heading"
    elif block_type == "Text" or block_type == "TextInlineMath":
        return "paragraph"
    elif block_type in VISUAL_IMAGE_BLOCK_TYPES:
        return "image"
    elif block_type == "ListGroup":
        return "list"
    elif block_type == "ListItem":
        return "list_item_internal"
    elif block_type == "Table" or block_type == "TableGroup":
        return "table"
    elif block_type == "Code":
        return "code"
    elif block_type == "Equation":
        return "equation"
    elif block_type == "Caption":
        return "paragraph" # Or a more specific "image_description" if desired later
    return None

# --- Main Processing Functions ---
def extract_elements_from_marker(marker_data_input):
    global TEMP_IMAGE_BASE_DIR
    all_elements = []
    processed_list_group_ids = set()
    processed_parent_group_ids = set()

    page_objects_to_process = []
    if isinstance(marker_data_input, list):
        page_objects_to_process = marker_data_input
    elif isinstance(marker_data_input, dict):
        root_block_type = marker_data_input.get("block_type")
        if root_block_type == "Document":
            page_objects_to_process = marker_data_input.get("children", [])
        elif root_block_type == "Page":
            page_objects_to_process = [marker_data_input]
        else:
            print(f"Warning: Marker JSON root is dict of unhandled type: {root_block_type}.", file=sys.stderr)
            return all_elements
    else:
        print(f"Error: Marker JSON data is not list/dict. Type: {type(marker_data_input)}. Cannot process.", file=sys.stderr)
        return all_elements

    for page_num_idx, page_block in enumerate(page_objects_to_process):
        if not isinstance(page_block, dict) or page_block.get("block_type") != "Page":
            print(f"Warning: Item in page list is not a 'Page' dict (index {page_num_idx}). Skipping. Item: {page_block}", file=sys.stderr)
            continue

        page_id_str = page_block.get("id", "")
        original_page_number = page_num_idx + 1
        if page_id_str and isinstance(page_id_str, str):
            try:
                parts = page_id_str.split('/')
                if len(parts) > 2 and parts[1] == 'page':
                    original_page_number = int(parts[2]) + 1
            except (ValueError, IndexError): pass

        temp_blocks_on_page = []
        for child_block in page_block.get("children", []):
            if isinstance(child_block, dict):
                temp_blocks_on_page.append(child_block)
        
        i = 0
        while i < len(temp_blocks_on_page):
            child_block = temp_blocks_on_page[i]
            block_id = child_block.get("id")
            marker_block_type = child_block.get("block_type")

            if marker_block_type in IGNORED_BLOCK_TYPES or (block_id and block_id in processed_parent_group_ids) :
                i += 1
                continue

            slide_element_type = get_block_semantic_type(child_block)
            element_image_reference_path = None
            content = ""
            raw_b64_for_saving = None # Holds base64 string before saving to file

            if slide_element_type == "image":
                if marker_block_type == "FigureGroup" or marker_block_type == "PictureGroup":
                    figure_child, caption_child = None, None
                    if child_block.get("children"):
                        for sub_child in child_block.get("children", []):
                            if isinstance(sub_child, dict):
                                sub_type = sub_child.get("block_type")
                                if sub_type == "Figure" or sub_type == "Picture": figure_child = sub_child
                                elif sub_type == "Caption": caption_child = sub_child
                    if figure_child:
                        fig_id = figure_child.get("id")
                        if figure_child.get("images") and isinstance(figure_child["images"], dict) and fig_id in figure_child["images"]:
                            raw_b64_for_saving = figure_child["images"][fig_id]
                        current_content = get_clean_text(caption_child.get("html")) if caption_child else get_clean_text(child_block.get("html"))
                        if current_content.lower() != marker_block_type.lower(): content = current_content
                        if raw_b64_for_saving and block_id: processed_parent_group_ids.add(block_id)
                    if not raw_b64_for_saving: slide_element_type = None # No image found
                        
                elif marker_block_type == "Figure" or marker_block_type == "Picture":
                    if child_block.get("images") and isinstance(child_block["images"], dict) and block_id in child_block["images"]:
                        raw_b64_for_saving = child_block["images"][block_id]
                        current_content = get_clean_text(child_block.get("html"))
                        if current_content.lower() != marker_block_type.lower(): content = current_content
                    if not raw_b64_for_saving: slide_element_type = None

                if raw_b64_for_saving and TEMP_IMAGE_BASE_DIR:
                    try:
                        temp_image_filename = f"{uuid.uuid4()}.png" # Assuming PNG for now
                        temp_image_path = os.path.join(TEMP_IMAGE_BASE_DIR, temp_image_filename)
                        with open(temp_image_path, "wb") as img_file:
                            img_file.write(base64.b64decode(raw_b64_for_saving))
                        element_image_reference_path = temp_image_path
                    except Exception as e:
                        print(f"Error saving temp image for {block_id}: {e}", file=sys.stderr)
                        element_image_reference_path = None; slide_element_type = None
                elif raw_b64_for_saving and not TEMP_IMAGE_BASE_DIR:
                    print(f"Error: TEMP_IMAGE_BASE_DIR not init for {block_id}", file=sys.stderr)
                    slide_element_type = None
                if not raw_b64_for_saving and slide_element_type == "image": # If it was image type but no data
                    slide_element_type = None


            elif slide_element_type in ["table", "code", "equation"]:
                content = get_clean_text(child_block.get("html"))
                if content.lower() == marker_block_type.lower().replace("group", ""): content = ""
                if not content and child_block.get("children"):
                    child_texts = [get_clean_text(sc.get("html")) for sc in child_block.get("children",[]) if isinstance(sc,dict)]
                    content = "\n".join(filter(None, child_texts))

            elif slide_element_type == "list":
                if block_id in processed_list_group_ids: i += 1; continue
                list_items = [f"- {get_clean_text(item.get('html'))}" for item in child_block.get("children", []) if isinstance(item,dict) and item.get("block_type") == "ListItem"]
                content = "\n".join(list_items)
                if block_id: processed_list_group_ids.add(block_id)

            elif slide_element_type in ["paragraph", "heading"]:
                content = get_clean_text(child_block.get("html"))
            
            else: i += 1; continue

            if (content or element_image_reference_path) and slide_element_type:
                all_elements.append({
                    "id": str(uuid.uuid4()), "type": slide_element_type, "content": content,
                    "image_reference_path": element_image_reference_path,
                    "original_page_number": original_page_number,
                    "marker_block_type": marker_block_type, "marker_polygon": child_block.get("polygon")
                })
            i += 1
    return all_elements

def assemble_slides(elements):
    # print(f"assemble_slides called with {len(elements)} elements.", file=sys.stderr)
    slides_data = []
    if not elements: return slides_data

    current_slide_elements, current_slide_text_count, current_slide_image_count = [], 0, 0
    current_slide_source_pages = set()
    slide_counter = 0
    
    processed_elements = list(elements) # Make a copy
    if processed_elements and processed_elements[0]["type"] == "heading":
        processed_elements[0]["type"] = "title" # First heading becomes title
        # print(f"assemble_slides: Changed first element to title.", file=sys.stderr)

    def finalize_slide(reason=""):
        nonlocal slide_counter, current_slide_elements, current_slide_text_count, current_slide_image_count, current_slide_source_pages
        if current_slide_elements:
            slide_counter += 1
            # print(f"assemble_slides: Finalizing slide {slide_counter} due to: {reason}. Elements: {len(current_slide_elements)}", file=sys.stderr)
            output_slide_elements = []
            for idx, el_data in enumerate(current_slide_elements):
                final_image_data_b64_for_json = None
                if el_data["type"] == "image" and el_data.get("image_reference_path"):
                    image_path = el_data["image_reference_path"]
                    if os.path.exists(image_path):
                        try:
                            with open(image_path, "rb") as img_file:
                                final_image_data_b64_for_json = base64.b64encode(img_file.read()).decode('utf-8')
                        except Exception as e: print(f"Error reading temp image {image_path}: {e}", file=sys.stderr)
                    else: print(f"Warning: Temp image file not found: {image_path}", file=sys.stderr)
                
                output_slide_elements.append({
                    "id": el_data["id"], "type": el_data["type"], "content": el_data["content"],
                    "imageData": final_image_data_b64_for_json, "position": idx
                })
            slides_data.append({
                "id": str(uuid.uuid4()), "slideNumber": slide_counter, "elements": output_slide_elements,
                "metadata": {"sourcePageNumbers": sorted(list(current_slide_source_pages)), "timestamp": datetime.now(timezone.utc).isoformat()}
            })
            current_slide_elements, current_slide_text_count, current_slide_image_count = [], 0, 0
            current_slide_source_pages = set()
        # else: print(f"assemble_slides: Attempted to finalize slide but empty. Reason: {reason}", file=sys.stderr)

    for i, el in enumerate(processed_elements):
        # print(f"\nassemble_slides: Processing element {i+1}/{len(processed_elements)}: Type='{el['type']}'", file=sys.stderr)
        is_image_element = el["type"] == "image"
        is_text_element = el["type"] in ["title", "heading", "subheading", "paragraph", "list", "table", "code", "equation"]
        new_slide_needed = False

        if is_image_element:
            if current_slide_image_count >= MAX_IMAGES_PER_SLIDE: new_slide_needed = True
            elif current_slide_image_count == 0 and current_slide_text_count >= MAX_TEXT_WITH_IMAGES : new_slide_needed = True # Adding first image, but too much text already
        elif is_text_element:
            if current_slide_text_count >= MAX_TEXT_ELEMENTS_PER_SLIDE: new_slide_needed = True
            elif current_slide_image_count > 0 and current_slide_text_count >= MAX_TEXT_WITH_IMAGES: new_slide_needed = True
        
        if new_slide_needed and current_slide_elements:
            finalize_slide(f"constraints for element type '{el['type']}'")

        current_slide_elements.append(el)
        current_slide_source_pages.add(el["original_page_number"])
        if is_image_element: current_slide_image_count += 1
        elif is_text_element: current_slide_text_count += 1
        # print(f"  After adding: Texts={current_slide_text_count}, Images={current_slide_image_count}. Elements on slide: {len(current_slide_elements)}", file=sys.stderr)

    finalize_slide("end of all elements")
    # print(f"assemble_slides: Returning {len(slides_data)} slides.", file=sys.stderr)
    return slides_data

def create_document_json(slides, marker_json_path, marker_meta_json_path, original_pdf_path, total_pdf_pages_calculated):
    filename_base = os.path.splitext(os.path.basename(original_pdf_path))[0]
    doc_title = filename_base; doc_author = None
    if os.path.exists(marker_meta_json_path):
        try:
            with open(marker_meta_json_path, 'r', encoding='utf-8') as f:
                meta_data_root = json.load(f)
                if isinstance(meta_data_root, dict):
                    doc_title = meta_data_root.get("title", doc_title)
                    doc_author = meta_data_root.get("author")
        except json.JSONDecodeError: print(f"Warning: Could not decode {marker_meta_json_path}", file=sys.stderr)
    if (doc_title == filename_base or not doc_title) and slides and slides[0]["elements"] and slides[0]["elements"][0]["type"] == "title":
        doc_title = slides[0]["elements"][0]["content"]
    file_size = os.path.getsize(original_pdf_path) if os.path.exists(original_pdf_path) else 0
    return {
        "id": str(uuid.uuid4()), "title": doc_title if doc_title else "Untitled Document", "author": doc_author,
        "createdAt": datetime.now(timezone.utc).isoformat(), "lastViewedAt": datetime.now(timezone.utc).isoformat(),
        "lastViewedSlide": 0, "slides": slides, "totalPages": total_pdf_pages_calculated, "fileSize": file_size,
        "localPath": original_pdf_path, "cloudSyncStatus": "notSynced",
        "processingMetadata": {"processingTime": 0.0, "modelUsed": "Marker + Custom Converter", "parserVersion": "1.3", "confidence": None}
    }

def run_marker(pdf_path, output_dir_for_marker_files):
    # Marker will create its .json and _meta.json in output_dir_for_marker_files
    os.makedirs(output_dir_for_marker_files, exist_ok=True)
    # Adjust command if your marker CLI is different or needs specific model paths for M-chip
    cmd = ["marker_single", pdf_path, "--output_dir" , output_dir_for_marker_files,"--output_format","json"] # Small batch for single file
    print(f"Running Marker: {' '.join(cmd)}", file=sys.stderr) # Print to stderr
    try:
        # Let Marker's stdout/stderr pass through to this script's stdout/stderr
        # This avoids buffering large amounts of data in 'process.stdout'/'process.stderr'
        # Marker usually prints progress to stderr.
        process = subprocess.run(cmd, check=True, env=MY_ENV, text=True, encoding='utf-8') # Removed capture_output=True

        # Since we are not capturing output, process.stdout and process.stderr will be None.
        # The success or failure is determined by check=True raising an error.
        print(f"Marker process completed with return code: {process.returncode}", file=sys.stderr)

        base_name = os.path.splitext(os.path.basename(pdf_path))[0]
        marker_json_out = os.path.join(output_dir_for_marker_files, base_name,f"{base_name}.json")
        marker_meta_out = os.path.join(output_dir_for_marker_files, base_name,f"{base_name}_meta.json")
        if not os.path.exists(marker_json_out):
            raise FileNotFoundError(f"Marker output JSON not found: {marker_json_out}")
        return marker_json_out, marker_meta_out
        
    except subprocess.CalledProcessError as e:
        # e.stdout and e.stderr will be None here because we didn't capture
        print(f"Error running Marker. Command '{' '.join(e.cmd)}' returned non-zero exit status {e.returncode}.", file=sys.stderr)
        # If you need to see Marker's output on error, you might have to re-run with capture_output=True
        # or ensure Marker logs to a file that you can then read.
        raise
    except FileNotFoundError: # For marker_cli not found
        print("Error: marker_cli command not found. Make sure Marker is installed and in your PATH.", file=sys.stderr)
        raise
    except Exception as e: # Catch other potential errors
        print(f"An unexpected error occurred while running Marker: {e}", file=sys.stderr)
        raise

def main():
    global TEMP_IMAGE_BASE_DIR
    parser = argparse.ArgumentParser(description="Convert PDF to structured slides JSON using Marker.")
    parser.add_argument("pdf_path", help="Path to the input PDF file.")
    parser.add_argument("--output_dir", default="marker_output", help="Directory for Marker's intermediate JSON files (e.g., filename.json).")
    parser.add_argument("--skip_marker", action="store_true", help="Skip running Marker, assume its output files exist in output_dir.")
    parser.add_argument("--marker_json_path", help="Direct path to Marker's .json output (used if --skip_marker).")
    parser.add_argument("--marker_meta_json_path", help="Direct path to Marker's _meta.json output (used if --skip_marker).")
    parser.add_argument("--save_json_to", help="Optional: Path to save the final structured slide JSON to a file.")
    parser.add_argument("--temp_dir_path", help="Optional: Base path for script's temporary image files (a sub-folder will be created here).")

    parser.add_argument("--final_json_output_path", help="Path to save the final structured slide JSON to a file. If provided, output might not go to stdout unless --force_stdout is also used.")
    parser.add_argument("--no_stdout", action="store_true", help="Do not print the final JSON to standard output. Useful if only saving to a file.")

    args = parser.parse_args()

    start_time = datetime.now()
    
    temp_image_dir_created_by_script = None # To track the unique dir we make

    try:
        base_for_temp = args.temp_dir_path if args.temp_dir_path and os.path.isdir(args.temp_dir_path) else tempfile.gettempdir()
        TEMP_IMAGE_BASE_DIR = tempfile.mkdtemp(prefix="slide_app_images_", dir=base_for_temp)
        temp_image_dir_created_by_script = TEMP_IMAGE_BASE_DIR # Store to ensure we clean this one
        print(f"Using temporary image directory: {TEMP_IMAGE_BASE_DIR}", file=sys.stderr)
    except Exception as e:
        print(f"Fatal: Could not create temporary image directory: {e}. Exiting.", file=sys.stderr)
        TEMP_IMAGE_BASE_DIR = None
        return 1 # Indicate error

    if not os.path.exists(args.pdf_path):
        print(f"Error: PDF file not found at {args.pdf_path}", file=sys.stderr)
        return 1

    marker_json_path = args.marker_json_path
    marker_meta_json_path = args.marker_meta_json_path
    
    # Ensure output_dir for Marker exists if we are running Marker
    if not args.skip_marker:
        if not os.path.exists(args.output_dir):
            try:
                os.makedirs(args.output_dir, exist_ok=True)
            except OSError as e:
                print(f"Error creating Marker output directory {args.output_dir}: {e}", file=sys.stderr)
                return 1 
        try:
            marker_json_path, marker_meta_json_path = run_marker(args.pdf_path, args.output_dir)
        except Exception as e:
            print(f"Failed to run Marker: {e}", file=sys.stderr)
            return 1
    else: # Logic for skipped marker execution
        if not marker_json_path:
            base_name = os.path.splitext(os.path.basename(args.pdf_path))[0]
            marker_json_path = os.path.join(args.output_dir, base_name,f"{base_name}.json")
        if not marker_meta_json_path:
            base_name = os.path.splitext(os.path.basename(args.pdf_path))[0]
            marker_meta_json_path = os.path.join(args.output_dir, base_name,f"{base_name}_meta.json")

    if not os.path.exists(marker_json_path):
        print(f"Error: Marker JSON file not found at {marker_json_path}.", file=sys.stderr)
        return 1
    if not os.path.exists(marker_meta_json_path):
        print(f"Warning: Marker meta JSON file not found at {marker_meta_json_path}. Proceeding without it.", file=sys.stderr)

    try:
        with open(marker_json_path, 'r', encoding='utf-8') as f:
            marker_data_from_json = json.load(f)
    except Exception as e:
        print(f"Error reading/decoding Marker JSON file {marker_json_path}: {e}", file=sys.stderr)
        return 1

    total_pdf_pages_calculated = 0
    if isinstance(marker_data_from_json, list):
        total_pdf_pages_calculated = len([p for p in marker_data_from_json if isinstance(p, dict) and p.get("block_type") == "Page"])
    elif isinstance(marker_data_from_json, dict):
        if marker_data_from_json.get("block_type") == "Document":
            children = marker_data_from_json.get("children", [])
            total_pdf_pages_calculated = len([p for p in children if isinstance(p, dict) and p.get("block_type") == "Page"])
        elif marker_data_from_json.get("block_type") == "Page": total_pdf_pages_calculated = 1
    
    extracted_elements = extract_elements_from_marker(marker_data_from_json)
    slides = assemble_slides(extracted_elements)
    final_document_json = create_document_json(slides, marker_json_path, marker_meta_json_path, args.pdf_path, total_pdf_pages_calculated)
    end_time = datetime.now()
    final_document_json["processingMetadata"]["processingTime"] = round((end_time - start_time).total_seconds(), 3)

    
    # Print exceution time
    print(f"\nExecution time: {round((end_time - start_time).total_seconds(), 3)} seconds", file=sys.stderr)


    output_json_string = json.dumps(final_document_json, indent=2)
    
    if args.final_json_output_path:
        try:
            with open(args.final_json_output_path, 'w', encoding='utf-8') as f:
                f.write(output_json_string)
            print(f"Saved final JSON output to: {args.final_json_output_path}", file=sys.stderr)
        except IOError as e:
            print(f"Error saving final JSON to file {args.final_json_output_path}: {e}", file=sys.stderr)
            # Decide if this is a fatal error or if stdout can still proceed
            # For now, let it proceed to stdout if not suppressed.

    if not args.no_stdout:
        print(output_json_string) # FINAL JSON TO STDOUT (if not suppressed)
    elif args.no_stdout and not args.final_json_output_path:
        print("Warning: --no_stdout used without --final_json_output_path. No output produced.", file=sys.stderr)

    # Cleanup temporary image directory created by this script run
    if temp_image_dir_created_by_script and os.path.exists(temp_image_dir_created_by_script):
        try:
            shutil.rmtree(temp_image_dir_created_by_script)
            print(f"Successfully cleaned up temp image directory: {temp_image_dir_created_by_script}", file=sys.stderr)
        except Exception as e:
            print(f"Warning: Failed to clean up temp image directory {temp_image_dir_created_by_script}: {e}", file=sys.stderr)
    
    return 0 # Success

if __name__ == "__main__":
    # All informational/debug prints from the script now go to stderr.
    # The final JSON goes to stdout.
    # This makes it cleaner for programmatic capture by Swift.
    exit_code = main()
    sys.exit(exit_code)