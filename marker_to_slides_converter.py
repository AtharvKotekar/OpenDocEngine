import json
import uuid
import os
import sys
from datetime import datetime, timezone
from bs4 import BeautifulSoup # For parsing HTML content from Marker
import argparse
import subprocess # To potentially call Marker from Python

# --- Configuration & Mapping ---

# Block types to ignore completely
IGNORED_BLOCK_TYPES = {"PageFooter", "PageHeader", "Footnote", "Form", "Handwriting", "TableOfContents"}

# Block types that are primarily visual and should be extracted as images IF image data is present
# FigureGroup and PictureGroup often contain actual Figure/Picture children with image data.
VISUAL_IMAGE_BLOCK_TYPES = {"Figure", "Picture", "FigureGroup", "PictureGroup"}

# Block types that are structural but we want to treat as textual content for now
# Marker might provide HTML/Markdown for tables, LaTeX for equations, and text for code.
TEXTUAL_STRUCTURAL_BLOCK_TYPES = {"Table", "TableGroup", "Code", "Equation"}

# Slide constraints
MAX_TEXT_ELEMENTS_PER_SLIDE = 3
MAX_IMAGES_PER_SLIDE = 2
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
    section_hierarchy = marker_block.get("section_hierarchy")

    # IMPORTANT: Fix string comparison
    if block_type == "SectionHeader": # Changed from 'is'
        # ... (rest of the function as provided previously) ...
        pass # Placeholder for brevity
    elif block_type == "Text" or block_type == "TextInlineMath": # Changed from 'is' for "Text"
        return "paragraph"
    elif block_type in VISUAL_IMAGE_BLOCK_TYPES: # Use the new category
        return "image"
    elif block_type == "ListGroup":
        return "list"
    elif block_type == "ListItem":
        return "list_item_internal" # Handled by parent ListGroup
    elif block_type == "Table" or block_type == "TableGroup": # New mapping
        return "table"
    elif block_type == "Code": # New mapping
        return "code"
    elif block_type == "Equation": # New mapping
        return "equation"
    elif block_type == "Caption": # Explicitly handle captions
        return "paragraph" 
    return None

# --- Main Processing Functions NEXT ---


def extract_elements_from_marker(marker_data_input):
    all_elements = []
    processed_list_group_ids = set()
    processed_parent_group_ids = set() # To avoid re-processing children of groups we already handled

    page_objects_to_process = []
    if isinstance(marker_data_input, list):
        page_objects_to_process = marker_data_input
    elif isinstance(marker_data_input, dict):
        # ... (same logic as before to get page_objects_to_process) ...
        root_block_type = marker_data_input.get("block_type")
        if root_block_type == "Document":
            page_objects_to_process = marker_data_input.get("children", [])
        elif root_block_type == "Page":
            page_objects_to_process = [marker_data_input]
        else:
            print(f"Warning: Marker JSON root is dict of unhandled type: {root_block_type}.")
            return all_elements
    else:
        print(f"Error: Marker JSON data is not list/dict. Type: {type(marker_data_input)}. Cannot process.")
        return all_elements

    for page_num_idx, page_block in enumerate(page_objects_to_process):
        if not isinstance(page_block, dict) or page_block.get("block_type") != "Page":
            # ... (same warning logic as before) ...
            print(f"Warning: Item in page list is not a 'Page' dict (index {page_num_idx}). Skipping. Item: {page_block}")
            continue

        page_id_str = page_block.get("id", "")
        original_page_number = page_num_idx + 1 # Default
        # ... (same page number extraction logic as before) ...
        if page_id_str and isinstance(page_id_str, str):
            try:
                parts = page_id_str.split('/')
                if len(parts) > 2 and parts[1] == 'page':
                    original_page_number = int(parts[2]) + 1
            except (ValueError, IndexError): pass # Keep default if parsing fails


        # Iterate through top-level children of the Page
        page_children = page_block.get("children", [])
        
        # --- MODIFIED BLOCK ITERATION ---
        # We need to process blocks sequentially, but handle groups by looking at their children
        
        blocks_to_process_on_page = []
        # First pass to flatten or identify direct processable blocks
        temp_blocks_on_page = []
        for child_block in page_children:
            if not isinstance(child_block, dict): continue
            temp_blocks_on_page.append(child_block)

        i = 0
        while i < len(temp_blocks_on_page):
            child_block = temp_blocks_on_page[i]
            block_id = child_block.get("id")
            marker_block_type = child_block.get("block_type")

            if marker_block_type in IGNORED_BLOCK_TYPES or block_id in processed_parent_group_ids:
                i += 1
                continue

            slide_element_type = get_block_semantic_type(child_block)
            image_data_b64 = None
            content = ""

            # --- IMAGE EXTRACTION REWORK ---
            if slide_element_type == "image":
                # If it's a FigureGroup or PictureGroup, look for image in its children
                if marker_block_type == "FigureGroup" or marker_block_type == "PictureGroup":
                    figure_child = None
                    caption_child = None
                    if child_block.get("children"):
                        for sub_child in child_block.get("children"):
                            if not isinstance(sub_child, dict): continue
                            sub_child_type = sub_child.get("block_type")
                            if sub_child_type == "Figure" or sub_child_type == "Picture":
                                figure_child = sub_child
                            elif sub_child_type == "Caption":
                                caption_child = sub_child
                    
                    if figure_child:
                        fig_id = figure_child.get("id")
                        if figure_child.get("images") and isinstance(figure_child["images"], dict) and fig_id in figure_child["images"]:
                            image_data_b64 = figure_child["images"][fig_id]
                        # Try to get caption from a sibling Caption block or FigureGroup's own HTML
                        if caption_child:
                            content = get_clean_text(caption_child.get("html"))
                        elif not content: # If no caption child, try FigureGroup's html
                             html_content = get_clean_text(child_block.get("html"))
                             # Avoid generic "Figure" or "Table" as caption
                             if html_content.lower() != marker_block_type.lower():
                                 content = html_content
                        
                        if image_data_b64: # Mark parent group as processed
                             if block_id: processed_parent_group_ids.add(block_id)
                        else: # No image found even in children
                            print(f"Warning: {marker_block_type} {block_id} or its Figure/Picture children had no image data.")
                            slide_element_type = None # Don't create an image element
                            
                # If it's a direct Figure or Picture (not inside a processed group)
                elif marker_block_type == "Figure" or marker_block_type == "Picture":
                    if child_block.get("images") and isinstance(child_block["images"], dict) and block_id in child_block["images"]:
                        image_data_b64 = child_block["images"][block_id]
                        html_content = get_clean_text(child_block.get("html"))
                        if html_content.lower() != marker_block_type.lower():
                            content = html_content # Use its own HTML as potential caption
                    else:
                        print(f"Warning: Direct {marker_block_type} {block_id} had no image data.")
                        slide_element_type = None # Don't create an image element
                else: # Should not happen if mapping is correct
                    slide_element_type = None
            
            # --- TEXTUAL STRUCTURAL TYPES (Table, Code, Equation) ---
            elif slide_element_type in ["table", "code", "equation"]:
                # For these, we primarily want their textual/HTML content from Marker
                content = get_clean_text(child_block.get("html"))
                # Marker might put LaTeX for equations here, or structured HTML for tables.
                # If content is just the block type name (e.g. "Table", "Equation"), clear it
                if content.lower() == marker_block_type.lower().replace("group",""): # "TableGroup" -> "table"
                    content = ""
                if not content and child_block.get("children"): # Try children for TableGroup
                    child_texts = []
                    for sub_c in child_block.get("children"):
                        if isinstance(sub_c, dict):
                             child_texts.append(get_clean_text(sub_c.get("html")))
                    content = "\n".join(filter(None, child_texts))


            # --- LISTS ---
            elif slide_element_type == "list":
                if block_id in processed_list_group_ids:
                    i += 1
                    continue
                list_items_text = []
                if child_block.get("children"):
                    for item_block in child_block.get("children", []):
                        if isinstance(item_block, dict) and item_block.get("block_type") == "ListItem":
                            list_items_text.append(f"- {get_clean_text(item_block.get('html'))}")
                content = "\n".join(list_items_text)
                if block_id: processed_list_group_ids.add(block_id)

            # --- OTHER TEXT-BASED (Paragraph, Heading, Title (from heading)) ---
            elif slide_element_type in ["paragraph", "heading"]: # title is derived from heading
                content = get_clean_text(child_block.get("html"))
            
            else: # Unhandled or explicitly ignored type
                i += 1
                continue # Move to next block

            # Add element if it has content or image data
            if (content or image_data_b64) and slide_element_type:
                all_elements.append({
                    "id": str(uuid.uuid4()),
                    "type": slide_element_type,
                    "content": content,
                    "imageData_b64": image_data_b64,
                    "original_page_number": original_page_number,
                    "marker_block_type": marker_block_type,
                    "marker_polygon": child_block.get("polygon")
                })
            i += 1
            # --- END MODIFIED BLOCK ITERATION ---

    return all_elements

def assemble_slides(elements):
    print(f"assemble_slides called with {len(elements)} elements.") # DEBUG
    slides_data = []
    if not elements:
        print("assemble_slides: No elements to process, returning empty slides_data.") # DEBUG
        return slides_data

    current_slide_elements = []
    current_slide_text_count = 0
    current_slide_image_count = 0
    current_slide_source_pages = set()
    slide_counter = 0

    # Make a copy for modification (title heuristic)
    processed_elements = list(elements)
    if processed_elements and processed_elements[0]["type"] == "heading":
        # Heuristic: make the very first heading element a "title"
        # Check if it's truly the first element of the document,
        # not just first on a subsequent page if elements were sorted differently.
        # For now, this simple check is fine.
        processed_elements[0]["type"] = "title"
        print(f"assemble_slides: Changed first element to title: {processed_elements[0]['content'][:30]}") # DEBUG

    def finalize_slide(reason=""):
        nonlocal slide_counter, current_slide_elements, current_slide_text_count, current_slide_image_count, current_slide_source_pages
        if current_slide_elements:
            slide_counter += 1
            print(f"assemble_slides: Finalizing slide {slide_counter} due to: {reason}. Elements: {len(current_slide_elements)}, Text: {current_slide_text_count}, Images: {current_slide_image_count}") # DEBUG
            slides_data.append({
                "id": str(uuid.uuid4()),
                "slideNumber": slide_counter,
                "elements": [
                    {
                        "id": el["id"],
                        "type": el["type"],
                        "content": el["content"],
                        "imageData": el.get("imageData_b64"), # Corresponds to Swift's Data?
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
        else:
            print(f"assemble_slides: Attempted to finalize slide but current_slide_elements is empty. Reason: {reason}") # DEBUG


    for i, el in enumerate(processed_elements):
        print(f"\nassemble_slides: Processing element {i+1}/{len(processed_elements)}: Type='{el['type']}', Content='{el['content'][:50].replace('\n', ' ')}...'") # DEBUG
        is_image_element = el["type"] == "image"
        # For slide constraints, treat new textual types like paragraphs
        is_text_element = el["type"] in ["title", "heading", "subheading", "paragraph", "list", "table", "code", "equation"]

        new_slide_needed = False

        # Check if adding THIS element would violate constraints
        if is_image_element:
            # If current slide already has max images
            if current_slide_image_count >= MAX_IMAGES_PER_SLIDE:
                print(f"  Condition: New slide needed (max images {MAX_IMAGES_PER_SLIDE} already on current slide).") # DEBUG
                new_slide_needed = True
            # If current slide has text, and adding this image would violate text_with_image limit
            elif current_slide_text_count > 0 and MAX_TEXT_WITH_IMAGES == 0 : # if any text and no text allowed with images
                print(f"  Condition: New slide needed (no text allowed with images, and current slide has text).") # DEBUG
                new_slide_needed = True
            # elif current_slide_text_count >= MAX_TEXT_WITH_IMAGES and MAX_IMAGES_PER_SLIDE > 0:
            # This condition was a bit confusing. Let's simplify:
            # If this is the *first* image, and text is already at MAX_TEXT_WITH_IMAGES, new slide
            elif current_slide_image_count == 0 and current_slide_text_count >= MAX_TEXT_WITH_IMAGES:
                 print(f"  Condition: New slide needed (adding first image, but text already at {current_slide_text_count}/{MAX_TEXT_WITH_IMAGES} for image slides).") #DEBUG
                 new_slide_needed = True


        elif is_text_element:
            # If current slide already has max text elements
            if current_slide_text_count >= MAX_TEXT_ELEMENTS_PER_SLIDE:
                print(f"  Condition: New slide needed (max text elements {MAX_TEXT_ELEMENTS_PER_SLIDE} already on current slide).") # DEBUG
                new_slide_needed = True
            # If current slide has images, and adding this text would violate text_with_image limit
            elif current_slide_image_count > 0 and current_slide_text_count >= MAX_TEXT_WITH_IMAGES:
                print(f"  Condition: New slide needed (current slide has images, and adding this text would exceed {MAX_TEXT_WITH_IMAGES} text elements).") # DEBUG
                new_slide_needed = True

        # If a new slide is needed and there are elements on the current one, finalize it
        if new_slide_needed and current_slide_elements:
            finalize_slide(f"constraints for element type '{el['type']}'")
            # Reset counts for the new slide that will start with this element
            # current_slide_text_count = 0 # This will be reset by finalize_slide
            # current_slide_image_count = 0 # This will be reset by finalize_slide

        # Add the current element to the (potentially new) slide
        print(f"  Adding element to current_slide_elements. Before add: {len(current_slide_elements)} items.") # DEBUG
        current_slide_elements.append(el)
        current_slide_source_pages.add(el["original_page_number"])
        if is_image_element:
            current_slide_image_count += 1
        elif is_text_element:
            current_slide_text_count += 1
        print(f"  After adding: Texts={current_slide_text_count}, Images={current_slide_image_count}. Elements on slide: {len(current_slide_elements)}") # DEBUG


    # Finalize any remaining slide after the loop
    print("assemble_slides: Loop finished.") # DEBUG
    finalize_slide("end of all elements")
    
    print(f"assemble_slides: Returning {len(slides_data)} slides.") # DEBUG
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
    parser.add_argument("--save_json_to", help="Optional: Path to save the output JSON to a file.")
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

    
    try: 
        with open(marker_json_path, 'r', encoding='utf-8') as f:
            marker_data_from_json = json.load(f) # This is a list of page blocks

    except json.JSONDecodeError as e:
        print(f"Error decoding Marker JSON file {marker_json_path}: {e}")
        return
    except Exception as e:
        print(f"Error reading Marker JSON file {marker_json_path}: {e}")
        return
    
    total_pdf_pages_calculated = 0

    if isinstance(marker_data_from_json, list):
        total_pdf_pages_calculated = len([p for p in marker_data_from_json if isinstance(p, dict) and p.get("block_type") == "Page"])
    elif isinstance(marker_data_from_json, dict):
        if marker_data_from_json.get("block_type") == "Document":
            children = marker_data_from_json.get("children", [])
            total_pdf_pages_calculated = len([p for p in children if isinstance(p, dict) and p.get("block_type") == "Page"])
        elif marker_data_from_json.get("block_type") == "Page":
            total_pdf_pages_calculated = 1
        else:
            print(f"Warning: Cannot accurately determine total PDF pages from root dictionary type '{marker_data_from_json.get('block_type')}'. Setting to 0.")
    else:
        print(f"Warning: Cannot determine total PDF pages from data type {type(marker_data_from_json)}. Setting to 0.")

    extracted_elements = extract_elements_from_marker(marker_data_from_json)

    slides = assemble_slides(extracted_elements)

    final_document_json = create_document_json(slides, marker_json_path, marker_meta_json_path, args.pdf_path, total_pdf_pages_calculated)

    end_time = datetime.now()
    processing_time = (end_time - start_time).total_seconds()

    final_document_json["processingMetadata"]["processingTime"] = round(processing_time, 3)

    # Output the final JSON to stdout

    output_json_string = json.dumps(final_document_json, indent=2)

    print(output_json_string)

    # Optionally save to file if argument is provided
    if args.save_json_to:
        try:
            with open(args.save_json_to, 'w', encoding='utf-8') as f:
                f.write(output_json_string)
            print(f"\nAlso saved output to: {args.save_json_to}", file=sys.stderr) # Info message to stderr
        except IOError as e:
            print(f"\nError saving output to file {args.save_json_to}: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()