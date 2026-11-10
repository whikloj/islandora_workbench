import datetime
import logging
import os
import shutil
import sys

from islandora_workbench.data_io.csv.csv_reader import CsvReader


def create_contact_sheet_thumbnail(config: dict, source_filename: str) -> str:
    """Determines the thumbnail image to use for a given filename, and copies
    the image to the output directory.
    Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
        source_filename: string
            The value of the CSV row's "file" column, or the reserved value "compound".
        Returns
        -------
        string
            The file name of the thumbnail image file.
    """
    generic_icons_dir = os.path.join("assets", "contact_sheet", "generic_icons")

    if len(source_filename.strip()) == 0:
        no_file_icon_filename = "tn_generic_no_file.png"
        no_file_icon_path = os.path.join(
            config["contact_sheet_output_dir"], no_file_icon_filename
        )
        if not os.path.exists(no_file_icon_path):
            shutil.copyfile(
                os.path.join(generic_icons_dir, no_file_icon_filename),
                no_file_icon_path,
            )
        return no_file_icon_filename

    if source_filename == "compound":
        compound_icon_filename = "tn_generic_compound.png"
        compound_icon_path = os.path.join(
            config["contact_sheet_output_dir"], compound_icon_filename
        )
        if not os.path.exists(compound_icon_path):
            shutil.copyfile(
                os.path.join(generic_icons_dir, compound_icon_filename),
                compound_icon_path,
            )
        return compound_icon_filename

    pdf_extensions = [".pdf"]
    media_types = config.get("media_types", {})
    video_extensions = (
        [f".{x}" for x in media_types["video"]]
        if "video" in media_types
        else [".mp4", ".mov", ".avi"]
    )
    audio_extensions = (
        [f".{x}" for x in media_types["audio"]]
        if "audio" in media_types
        else [".mp3", ".wav", ".aac"]
    )
    image_extensions = (
        [f".{x}" for x in media_types["image"]]
        if "image" in media_types
        else [".png", ".jpg", ".jpeg", ".tif", ".tiff", ".jp2"]
    )

    source_file_name, source_file_extension = os.path.splitext(source_filename)
    if source_file_extension.lower() in image_extensions:
        """
        # Note: this block can be used to generate thumbnails for images if "from PIL import Image".
        image_source_path = os.path.join(input_dir, source_filename)
        image_source = Image.open(image_source_path)
        image_tn = image_source.copy()
        image_tn.thumbnail((200, 200))
        filename_info = os.path.splitext(row['file'])
        tn_filename = f'tn_{filename_info[0]}.png'
        tn_filepath = os.path.join(output_dir, tn_filename)
        image_tn.save(tn_filepath)
        tn_filepath = tn_filename
        """
        image_icon_filename = "tn_generic_image.png"
        image_icon_path = os.path.join(
            config["contact_sheet_output_dir"], image_icon_filename
        )
        if not os.path.exists(image_icon_path):
            shutil.copyfile(
                os.path.join(generic_icons_dir, image_icon_filename), image_icon_path
            )
        tn_filepath = image_icon_filename
    elif source_file_extension.lower() in pdf_extensions:
        pdf_icon_filename = "tn_generic_pdf.png"
        pdf_icon_path = os.path.join(
            config["contact_sheet_output_dir"], pdf_icon_filename
        )
        if not os.path.exists(pdf_icon_path):
            shutil.copyfile(
                os.path.join(generic_icons_dir, pdf_icon_filename), pdf_icon_path
            )
        tn_filepath = pdf_icon_filename
    elif source_file_extension.lower() in audio_extensions:
        audio_icon_filename = "tn_generic_audio.png"
        audio_icon_path = os.path.join(
            config["contact_sheet_output_dir"], audio_icon_filename
        )
        if not os.path.exists(audio_icon_path):
            shutil.copyfile(
                os.path.join(generic_icons_dir, audio_icon_filename), audio_icon_path
            )
        tn_filepath = audio_icon_filename
    elif source_file_extension.lower() in video_extensions:
        video_icon_filename = "tn_generic_video.png"
        video_icon_path = os.path.join(
            config["contact_sheet_output_dir"], video_icon_filename
        )
        if not os.path.exists(video_icon_path):
            shutil.copyfile(
                os.path.join(generic_icons_dir, video_icon_filename), video_icon_path
            )
        tn_filepath = video_icon_filename
    else:
        binary_icon_filename = "tn_generic_binary.png"
        binary_icon_path = os.path.join(
            config["contact_sheet_output_dir"], binary_icon_filename
        )
        if not os.path.exists(binary_icon_path):
            shutil.copyfile(
                os.path.join(generic_icons_dir, binary_icon_filename), binary_icon_path
            )
        tn_filepath = binary_icon_filename

    return tn_filepath


def generate_contact_sheet_from_csv(config: dict) -> None:
    """Generates a contact sheet from CSV data.
    Parameters
        ----------
        config : dict
            The configuration settings defined by workbench_config.get_config().
    """
    css_file_path = config["contact_sheet_css_path"]
    css_file_name = os.path.basename(css_file_path)

    if not os.path.exists(config["contact_sheet_output_dir"]):
        try:
            os.mkdir(config["contact_sheet_output_dir"])
        except Exception as e:
            message = (
                'Path in configuration option "contact_sheet_output_dir" ("'
                + config["contact_sheet_output_dir"]
                + '") is not writable.'
            )
            logging.error(message + " " + str(e))
            sys.exit("Error: " + message + " See log for more detail.")

    csv_reader = CsvReader(config)
    csv_data = csv_reader.get_csv_data()

    compound_items = list()
    csv_data_to_get_children = csv_reader.get_csv_data()
    if (
        config["paged_content_from_directories"] is True
        or config["paged_content_from_directories_parents_exist"] is True
    ):
        # Collect the IDs of top-level items for use in the "Using subdirectories" method
        # of creating compound/paged content.
        for get_children_row in csv_data_to_get_children:
            compound_items.append(get_children_row[config["id_field"]])
    else:
        # Collect the IDs of items whose IDs are in other (child) items' "parent_id" column,
        # a.k.a. compound items created using the "With page/child-level metadata" method.
        if "parent_id" in csv_reader.get_field_names():
            for get_children_row in csv_data_to_get_children:
                compound_items.append(get_children_row["parent_id"])

        deduplicated_compound_items = list(set(compound_items))
        compound_items = deduplicated_compound_items
        if "" in compound_items:
            compound_items.remove("")

    # Create a dict containing all the output file data.
    contact_sheet_output_files = dict()
    contact_sheet_output_files["main_contact_sheet"] = {}
    contact_sheet_output_files["main_contact_sheet"]["path"] = os.path.join(
        config["contact_sheet_output_dir"], "contact_sheet.htm"
    )
    contact_sheet_output_files["main_contact_sheet"]["file_handle"] = open(
        contact_sheet_output_files["main_contact_sheet"]["path"], "w"
    )
    contact_sheet_output_files["main_contact_sheet"]["markup"] = ""
    for compound_item_id in compound_items:
        if compound_item_id != "":
            contact_sheet_output_files[compound_item_id] = {}
            compound_item_contact_sheet_path = os.path.join(
                config["contact_sheet_output_dir"],
                f"{compound_item_id}_contact_sheet.htm",
            )
            contact_sheet_output_files[compound_item_id][
                "path"
            ] = compound_item_contact_sheet_path
            contact_sheet_output_files[compound_item_id]["file_handle"] = open(
                os.path.join(compound_item_contact_sheet_path), "w"
            )
            contact_sheet_output_files[compound_item_id]["markup"] = ""

    for output_file in contact_sheet_output_files.keys():
        contact_sheet_output_files[output_file]["file_handle"] = open(
            contact_sheet_output_files[output_file]["path"], "a"
        )
        contact_sheet_output_files[output_file]["file_handle"].write(
            f"<html>\n<head>\n<title>Islandora Workbench contact sheet</title>"
        )
        contact_sheet_output_files[output_file]["file_handle"].write(
            f'\n<meta http-equiv="Content-Type" content="text/html; charset=utf-8" />'
        )
        contact_sheet_output_files[output_file]["file_handle"].write(
            f'\n<link rel="stylesheet" media="all" href="{css_file_name}" />\n</head>\n'
        )

        if output_file != "main_contact_sheet":
            # "output_file" is the same as the CSV ID of the parent of items in the current contact sheet.
            members_of_div = (
                f'<div class="header">Members of '
                + f'CSV entry <a alt="Members of" href="contact_sheet.htm">{output_file}</a>.</div>\n'
            )
            contact_sheet_output_files[output_file]["file_handle"].write(members_of_div)

        contact_sheet_output_files[output_file]["file_handle"].write(
            '<div class="cards">\n'
        )

    for row in csv_data:
        if (
            config["paged_content_from_directories"] is True
            or config["paged_content_from_directories_parents_exist"] is True
        ):
            # Note: parent items (i.e. items with rows in the CSV) are processed below.
            if row[config["id_field"]] in compound_items:
                # Get all the page files for this parent and create a new contact sheet containing them.
                page_files_dir_path = os.path.join(
                    config["input_dir"], row[config["page_files_source_dir_field"]]
                )
                page_files = os.listdir(page_files_dir_path)

                # Page files need to be sorted by weight in the contact sheet.
                page_file_weights_map = {}
                for page_file_name in page_files:
                    weight = get_sequence_indicator_from_filename(
                        config, page_file_name
                    )
                    # Cast weight as int so we can sort it easily.
                    page_file_weights_map[int(weight)] = page_file_name
                    page_title = row["title"] + ", page " + weight

                sorted_weights = sorted(page_file_weights_map.keys())

                for page_sort_order in sorted_weights:
                    page_output_file = row[config["id_field"]]
                    filename_without_extension = os.path.splitext(
                        page_file_weights_map[page_sort_order]
                    )[0]
                    tn_filename = create_contact_sheet_thumbnail(
                        config, page_file_weights_map[page_sort_order]
                    )

                    # Start .card.
                    contact_sheet_output_files[page_output_file][
                        "markup"
                    ] = '\n<div class="card">\n'
                    contact_sheet_output_files[page_output_file][
                        "markup"
                    ] += f'<img alt="{filename_without_extension}" src="{tn_filename}" />'
                    # Start .fields
                    contact_sheet_output_files[page_output_file][
                        "markup"
                    ] += f'\n<div class="fields">'
                    contact_sheet_output_files[page_output_file]["markup"] += (
                        f'\n<div class="field system"><span class="field-label">file</span>: '
                        + f"{page_file_weights_map[page_sort_order]}</div>"
                    )
                    contact_sheet_output_files[page_output_file]["markup"] += (
                        f'\n<div class="field system"><span class="field-label">title</span>: '
                        + f"{page_title}</div>"
                    )
                    contact_sheet_output_files[page_output_file]["markup"] += (
                        f'\n<div class="field system"><span class="field-label">field_weight</span>: '
                        + f"{page_sort_order}</div>"
                    )
                    # Close .fields
                    contact_sheet_output_files[page_output_file][
                        "markup"
                    ] += f"\n<!-- .fields -->\n</div>"
                    # Close .card
                    contact_sheet_output_files[page_output_file][
                        "markup"
                    ] += f"\n<!-- .card -->\n</div>"
                    contact_sheet_output_files[page_output_file]["file_handle"].write(
                        contact_sheet_output_files[page_output_file]["markup"] + "\n"
                    )
        else:
            if "parent_id" in row:
                if row["parent_id"] == "":
                    output_file = "main_contact_sheet"
                    if row[config["id_field"]] in compound_items:
                        tn_filename = create_contact_sheet_thumbnail(config, "compound")
                    else:
                        tn_filename = create_contact_sheet_thumbnail(
                            config, row["file"]
                        )
                else:
                    output_file = row["parent_id"]
                    if row[config["id_field"]] in compound_items:
                        tn_filename = create_contact_sheet_thumbnail(config, "compound")
                    else:
                        tn_filename = create_contact_sheet_thumbnail(
                            config, row["file"]
                        )
            else:
                output_file = "main_contact_sheet"
                tn_filename = create_contact_sheet_thumbnail(config, row["file"])

        # During 'paged_content_from_directories' or 'paged_content_from_directories_parents_exist',
        # parent items (i.e. items with rows in the CSV) are processed from this point on.
        csv_id = row[config["id_field"]]
        title = row["title"]

        # Ensure that parent items get the compound icon.
        if (
            config["paged_content_from_directories"] is True
            or config["paged_content_from_directories_parents_exist"] is True
        ):
            output_file = "main_contact_sheet"
            tn_filename = create_contact_sheet_thumbnail(config, "compound")

        # TODO: output_file and tn_filename are declared in the above 'if', should this be indented inside the same block?
        # start .card
        contact_sheet_output_files[output_file]["markup"] = '\n<div class="card">\n'
        contact_sheet_output_files[output_file][
            "markup"
        ] += f'<img alt="{title}" src="{tn_filename}" />'

        # Start .fields
        contact_sheet_output_files[output_file]["markup"] += f'\n<div class="fields">'
        if row[config["id_field"]] in compound_items:
            contact_sheet_output_files[output_file]["markup"] += (
                f'<div class="field system"><span class="field-label"></span>'
                + f'<a alt="members" href="{row[config["id_field"]]}_contact_sheet.htm">members</a></div>'
            )
        contact_sheet_output_files[output_file][
            "markup"
        ] += f'\n<div class="field system"><span class="field-label">{config["id_field"]}</span>: {csv_id}</div>'
        if (
            config["paged_content_from_directories"] is False
            and config["paged_content_from_directories_parents_exist"] is False
        ) and len(row["file"]) > 0:
            contact_sheet_output_files[output_file][
                "markup"
            ] += f'\n<div class="field system"><span class="field-label">file</span>: {row["file"]}</div>'
        else:
            contact_sheet_output_files[output_file][
                "markup"
            ] += f'\n<div class="field system"><span class="field-label">file</span>:</div>'
        contact_sheet_output_files[output_file][
            "markup"
        ] += f'\n<div class="field"><span class="field-label">title:</span> {title}</div>'
        for fieldname in row:
            # These three fields have already been rendered.
            if fieldname not in [config["id_field"], "title", "file"]:
                if len(row[fieldname].strip()) == 0:
                    continue
                if len(row[fieldname]) > 30:
                    field_value = row[fieldname][:30]
                    row_value_with_enhanced_subdelimiter = row[fieldname].replace(
                        config["subdelimiter"], " &square; "
                    )
                    field_value = field_value.replace(
                        config["subdelimiter"], " &square; "
                    )
                    contact_sheet_output_files[output_file]["markup"] += (
                        f'\n<div class="field"><span class="field-label">{fieldname}:</span> '
                        + f'{field_value} <a href="" title="{row_value_with_enhanced_subdelimiter}">[...]</a></div>'
                    )
                else:
                    field_value = row[fieldname]
                    field_value = field_value.replace(
                        config["subdelimiter"], " &square; "
                    )
                    contact_sheet_output_files[output_file][
                        "markup"
                    ] += f'\n<div class="field"><span class="field-label">{fieldname}:</span> {field_value}</div>'
        # Close .fields
        contact_sheet_output_files[output_file][
            "markup"
        ] += f"\n<!-- .fields -->\n</div>"
        # Close .card
        contact_sheet_output_files[output_file]["markup"] += f"\n<!-- .card -->\n</div>"
        contact_sheet_output_files[output_file]["file_handle"].write(
            contact_sheet_output_files[output_file]["markup"] + "\n"
        )
        # Zero out the card markup before starting the next CSV row.
        contact_sheet_output_files[output_file]["markup"] = ""

    for output_file in contact_sheet_output_files.keys():
        # Close .cards
        contact_sheet_output_files[output_file]["file_handle"].write(
            f"\n<!-- .cards -->\n</div>"
        )
        contact_sheet_output_files[output_file]["file_handle"].write(
            '\n<div class="footer">Icons courtesy of <a href="https://icons8.com/">icons8</a>.</div>'
        )
        now = datetime.datetime.now()
        contact_sheet_output_files[output_file]["file_handle"].write(
            f'\n<div class="footer">Generated {now}.</div>'
        )
        contact_sheet_output_files[output_file]["file_handle"].write("\n</html>")
        contact_sheet_output_files[output_file]["file_handle"].close()

    shutil.copyfile(
        os.path.join(css_file_path),
        os.path.join(config["contact_sheet_output_dir"], css_file_name),
    )
