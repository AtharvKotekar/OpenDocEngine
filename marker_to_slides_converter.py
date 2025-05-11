import json
import uuid
import os
from datetime import datetime, timezone
from bs4 import BeautifulSoup # For parsing HTML content from Marker
import argparse
import subprocess # To potentially call Marker from Python

# --- Configuration & Mapping ---

# Block types to ignore completely
IGNORED_BLOCK_TYPES = {"PageFooter", "PageHeader", "Footnote", "Form", "Handwriting", "TableOfContents"}

# TODO: Might cause issue with tables, codes, and equations fix it.
# Block types to treat as images
IMAGE_LIKE_BLOCK_TYPES = {"Figure", "Picture", "Table", "Code", "Equation", "FigureGroup", "TableGroup", "PictureGroup"}

# Slide constraints
MAX_TEXT_ELEMENTS_PER_SLIDE = 3
MAX_IMAGES_PER_SLIDE = 1

# If a slide has images, what's the max text elements (e.g., for captions)?
MAX_TEXT_WITH_IMAGES = 1

def get_clean_text(html_content):
    """Extracts clean text from HTML string."""
    if not html_content:
        return ""
    
    soup = BeautifulSoup(html_content, "html.parser")

    # Replace <br> with newlines, then get text and strip extra whitespace
    for br in soup.find_all("br"):
        br.replace_with("\n")


    return soup.get_text(separator=" ", strip=True).replace("\n ", "\n").strip()


def get_block_semantic_type(marker_block):
    """Maps Marker block_type and section_hierarchy to our SlideElementType."""
    block_type = marker_block.get("block_type")
    section_hierarchy = marker_block.get("section_hierarchy") # e.g. {"1": "/page/0/SectionHeader/0"}

    if block_type is "SectionHeader":
        if section_hierarchy:
            # Assuming the key "1", "2", etc. indicates h1, h2
            # This logic can be refined, e.g. first "1" is title, subsequent "1"s are headings
            level = sorted(section_hierarchy.keys())[0]
            if level == "1":
                return "heading" # Could be "title" based on context (e.g., first prominent H1)
            elif level == "2":
                return "heading" # Or "subheading"
            else:
                return "subheading"
            
        return "heading" # Default for SectionHeader if no hierarchy info
    
    elif block_type is "Text" or block_type == "TextInlineMath": # Treat TextInlineMath as Text for now
        return "paragraph"
    

    # TODO: Might cause issue with tables, codes, and equations fix it.
    elif block_type in IMAGE_LIKE_BLOCK_TYPES:
        return "image"
    
    elif block_type in "ListGroup":
        return "list"
    
    elif block_type == "ListItem": # ListItems are handled by their parent ListGroup
        return "list_item_internal" # Special internal type
 
    # TODO : For now i'm skipping unkown blocks fix it
    return None 


def extract_elements_from_marker(marker_data):
    """
    Extracts and flattens all relevant blocks from Marker's JSON output,
    in their original reading order.
    """

    all_elements = []
    processed_list_group_ids = set()

    for page_num_index, page_block in enumerate(marker_data):

        # Skip ignored other block types except for Page
        if page_block.get("block_type") != "Page":
            continue

        original_page_number = int(page_block.get("id","").split('/')[2]) + 1 # Id format /page/X/...

        # Marker usually orders children by reading order, including columns so relying on it for now
        for child_block in page_block.get("children", []):
            block_id = child_block.get("id")
            marker_block_type = child_block.get("block_type")

            if marker_block_type in IGNORED_BLOCK_TYPES:
                continue

            slide_element_type = get_block_semantic_type(child_block)

            # TODO : For now i'm skipping unkown blocks fix it
            if not slide_element_type:
                continue

            content = ""
            image_data_b64 = None # Store as base64 string initially

            if slide_element_type == "list":
                # Already processed as part of a ListGroup
                if block_id in processed_list_group_ids:
                    continue

                # For ListGroup, concatenate text from its ListItem children
                list_items_text = []
                if child_block.get("children"):
                    for item_block in child_block.get("children", []):
                        if item_block.get("block_type") == "ListItem":
                            list_items_text.append(f"- {get_clean_text(item_block.get('html'))}")

                            content = "\n".join(list_items_text)
                            processed_list_group_ids.add(block_id)

            elif slide_element_type == "image":
                # Marker stores images in the 'images' dict of the block, keyed by block_id

                # TODO: Might cause while extracting image_data_b64 becuase it's in a dict with some key
                if child_block.get("images") and block_id in child_block["images"]:
                    image_data_b64 = child_block["images"][block_id]

                    # Try to get caption if available (Marker might put it in 'html' for some image types)
                    # Or if there's a 'Caption' block type nearby (more advanced logic needed)

                    caption_text = get_clean_text(child_block.get("html"))
                    # TODO: Currently using simple heriustic to determine if it's a caption
                    # This might need to be improved based on actual Marker output
                    content = caption_text if caption_text and caption_text != "Figure" and caption_text != "Table" else ""

                else:
                    # If no image data, maybe it's just a placeholder, skip or log
                    print(f"Warning: Image-like block {block_id} has no image data.")
                    continue

            else: # Text-based elements
                content = get_clean_text(child_block.get("html"))

                if not content and not image_data_b64: # Skip empty elements
                    continue

                all_elements.append({
                    "id": str(uuid.uuid4()),
                    "type": slide_element_type,
                    "content": content,
                    "imageData_b64": image_data_b64, # Will be converted to Data in Swift
                    "original_page_number": original_page_number,

                    "marker_block_type": marker_block_type, # For debugging/refinement
                    "marker_polygon": child_block.get("polygon") # For potential future use


                })

    return all_elements

def assemble_slides(elements):
    """
    Assembles extracted elements into slides based on defined constraints.
    """

    slides_data = []

    if not elements:
        return slides_data
    
    current_slide_elements = []
    current_slide_text_count = 0
    current_slide_image_count = 0
    current_slide_source_pages = set()
    slide_counter = 0

    def finalize_slide(reason=""):
        nonlocal slide_counter, current_slide_elements, current_slide_text_count, current_slide_image_count, current_slide_source_pages
        if current_slide_elements:

            print(f"Finalizing slide {slide_counter+1} due to: {reason}. Elements: {len(current_slide_elements)}, Text: {current_slide_text_count}, Images: {current_slide_image_count}")
            
            slide_counter += 1
            slides_data.append({
                "id": str(uuid.uuid4()),
                "slideNumber": slide_counter,
                "elements": [
                    {
                        "id": el["id"],
                        "type": el["type"],
                        "content": el["content"],
                        # imageData will be null if not an image, or if b64 string is null
                        "imageData": el.get("imageData_b64"),
                        "position": idx
                    } for idx, el in enumerate(current_slide_elements)
                ],
                "metadata": {
                    "sourcePageNumbers": sorted(list(current_slide_source_pages)),
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
            })

            current_slide_elements = []
            current_slide_text_count = 0
            current_slide_image_count = 0
            current_slide_source_pages = set()

            # Attempt to make the first prominent heading a "title" if it's the very first element
            if elements and elements[0]["type"] == "heading":
                elements[0]["type"] = "title"

            for i, el in enumerate(elements):
                is_image_element = el["type"] == "image"
                is_text_element = el["type"] in ["title", "heading", "subheading", "paragraph", "list"]


                # TODO: If element is too large (e.g. very long paragraph), force new slide
                # This is a heuristic; LLMs could do better semantic chunking.
                # For now, relying on Marker's block granularity.
                # if is_text_element and len(el["content"]) > 1000: # Arbitrary length
                # finalize_slide("long content")

                new_slide_needed = False

                if is_image_element:
                    if current_slide_image_count >= MAX_IMAGES_PER_SLIDE:
                        new_slide_needed = True
                        finalize_slide(f"max images ({MAX_IMAGES_PER_SLIDE}) reached")
                        # If adding this image would also mean too much text for an image-slide

                    elif current_slide_text_count >= MAX_TEXT_WITH_IMAGES and MAX_IMAGES_PER_SLIDE > 0 : 
                        # if slide already has text and we want to add an image
                        new_slide_needed = True
                        finalize_slide(f"too much text ({current_slide_text_count}) for an image slide")

                elif is_text_element:
                    if current_slide_text_count >= MAX_TEXT_ELEMENTS_PER_SLIDE:
                        new_slide_needed = True
                        finalize_slide(f"max text elements ({MAX_TEXT_ELEMENTS_PER_SLIDE}) reached")

                    # If slide already has images, restrict text elements
                    elif current_slide_image_count > 0 and current_slide_text_count >= MAX_TEXT_WITH_IMAGES:
                        new_slide_needed = True
                        finalize_slide(f"max text ({MAX_TEXT_WITH_IMAGES}) with images reached")

                    
                    if new_slide_needed and current_slide_elements: # Finalize existing before starting new
                        pass # Finalize already called

                    # Add element to current (potentially new) slide
                    current_slide_elements.append(el)
                    current_slide_source_pages.add(el["original_page_number"])
                    if is_image_element:
                        current_slide_image_count += 1
                    elif is_text_element:
                        current_slide_text_count += 1

                

        finalize_slide("end of elements") # Finalize any remaining slide
        return slides_data

def create_document_json(slides, marker_json_path, marker_meta_json_path, original_pdf_path, total_pdf_pages):
    """Creates the final Document JSON structure."""
    filename_base = os.path.splitext(os.path.basename(original_pdf_path))[0]
    doc_title = filename_base
    doc_author = None

    if os.path.exists(marker_meta_json_path):
        with open(marker_meta_json_path, 'r', encoding='utf-8') as f:
            meta_data_root = json.load(f) # Assuming meta_data might be a single object, not a list
            if isinstance(meta_data_root, list) and meta_data_root: # If it's a list like main .json
                 # Heuristic: Try to find a document title from first page's first prominent heading
                if slides and slides[0]["elements"] and slides[0]["elements"][0]["type"] == "title":
                     doc_title = slides[0]["elements"][0]["content"]
            elif isinstance(meta_data_root, dict): # If it's a dict with actual meta fields
                doc_title = meta_data_root.get("title", doc_title)
                # TODO: Probably not going to work because _meta.json comprises only table of contents
                doc_author = meta_data_root.get("author") if meta_data_root.get("author") else ""


    # If still no good title, and there's a "title" element in the first slide
    if doc_title == filename_base and slides and slides[0]["elements"] and slides[0]["elements"][0]["type"] == "title":
        doc_title = slides[0]["elements"][0]["content"]

    file_size = os.path.getsize(original_pdf_path) if os.path.exists(original_pdf_path) else 0

    document = {
        "id": str(uuid.uuid4()),
        "title": doc_title,
        "author": doc_author,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "lastViewedAt": datetime.now(timezone.utc).isoformat(),
        "lastViewedSlide": 0,
        "slides": slides,
        "totalPages": total_pdf_pages, # TODO Original PDF page count
        "fileSize": file_size,
        "localPath": original_pdf_path,
        "cloudSyncStatus": "notSynced",
        "processingMetadata": {
            "processingTime": 0.0, # Placeholder, calculate actual time in main()
            "modelUsed": "Marker + Custom Converter", # TODO Be more specific if Marker model version known
            "parserVersion": "1.0", # TODO Your converter version
            "confidence": None #TODO Marker doesn't provide this directly
        }
    }
    return document




def run_marker(pdf_path, output_dir):
    """
    Runs the Marker CLI tool.
    Assumes 'marker_cli' is in the system PATH or specifies the full path.
    """

    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)

    # Command to run Marker. Adjust if your marker_cli is located elsewhere.
    # You might need to specify model, batch size, etc.
    cmd = ["marker_single", pdf_path, "--output_dir", output_dir, "--output_format", "json"]

    print(f"Running Marker: {' '.join(cmd)}")

    try:
        process = subprocess.run(cmd, check=True, capture_output=True, text=True)
        print("Marker STDOUT:")
        print(process.stdout)

        if process.stderr:
            print("Marker STDERR:")
            print(process.stderr)
        print("Marker processing completed.")

        base_name = os.path.splitext(os.path.basename(pdf_path))[0]
        # Modified for directory structure
        marker_json_out = os.path.join(output_dir, base_name, f"{base_name}.json")
        marker_meta_out = os.path.join(output_dir, base_name, f"{base_name}_meta.json")


        if not os.path.exists(marker_json_out):
            raise FileNotFoundError(f"Marker output JSON not found: {marker_json_out}")


        return marker_json_out, marker_meta_out
    
    except subprocess.CalledProcessError as e:
        print(f"Error running Marker: {e}")
        print(f"Stdout: {e.stdout}")
        print(f"Stderr: {e.stderr}")
        raise

    except FileNotFoundError:
        print("Error: marker_cli command not found. Make sure Marker is installed and in your PATH.")
        print("You can install marker with: pip install marker-pdf")
        raise


def main():
    parser = argparse.ArgumentParser(description="Convert PDF to structured slides JSON using Marker.")
    parser.add_argument("pdf_path", help="Path to the input PDF file.")
    parser.add_argument("--output_dir", default="marker_output", help="Directory to store Marker's intermediate files.")
    parser.add_argument("--skip_marker", action="store_true", help="Skip running Marker, assume output files exist in output_dir.")
    parser.add_argument("--marker_json_path", help="Direct path to Marker's .json output (if --skip_marker).")
    parser.add_argument("--marker_meta_json_path", help="Direct path to Marker's _meta.json output (if --skip_marker).")


    args = parser.parse_args()

    start_time = datetime.now()

    if not os.path.exists(args.pdf_path):
        print(f"Error: PDF file not found at {args.pdf_path}")
        return
    
    marker_json_path = args.marker_json_path
    marker_meta_json_path = args.marker_meta_json_path

    if not args.skip_marker:
        try:
            marker_json_path, marker_meta_json_path = run_marker(args.pdf_path, args.output_dir)
        except Exception as e:
            print(f"Failed to run Marker: {e}")
            return
        
    else:
        if not marker_json_path or not marker_meta_json_path:
            base_name = os.path.splitext(os.path.basename(args.pdf_path))[0]
            if not marker_json_path:
                marker_json_path = os.path.join(args.output_dir, base_name, f"{base_name}.json")
            if not marker_meta_json_path:
                marker_meta_json_path = os.path.join(args.output_dir, base_name, f"{base_name}_meta.json")


        if not os.path.exists(marker_json_path):
            print(f"Error: Marker JSON file not found at {marker_json_path} (skipped Marker execution).")
            return
        if not os.path.exists(marker_meta_json_path):
            print(f"Warning: Marker meta JSON file not found at {marker_meta_json_path}. Proceeding without it.")
            # We can proceed without meta, but title/author might be less accurate.

    
    with open(marker_json_path, 'r', encoding='utf-8') as f:
        marker_data = json.load(f) # This is a list of page blocks

    total_pdf_pages = len(marker_data)
    extracted_elements = extract_elements_from_marker(marker_data)

    slides = assemble_slides(extracted_elements)

    final_document_json = create_document_json(slides, marker_json_path, marker_meta_json_path, args.pdf_path, total_pdf_pages)

    end_time = datetime.now()
    processing_time = (end_time - start_time).total_seconds()

    final_document_json["processingMetadata"]["processingTime"] = processing_time

    # Output the final JSON to stdout
    print(json.dumps(final_document_json, indent=2))

if __name__ == "__main__":
    main()