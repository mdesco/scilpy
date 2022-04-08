#!/usr/bin/env python
# -*- coding: utf-8 -*-


"""
Scores input tractogram overall and bundlewise. Outputs a results.json
containing a full report, a processing_stats.json containing information on the
formation of bundles (ex: the number of wpc per criteria), and splits the input
tractogram into one file per bundle : *_VS.tck. Remaining streamlines are
combined in a IS.tck file.

Definitions:
    In terms of number of streamlines:
        Computed by default:
        - VS: valid streamlines, belonging to a bundle (i.e. respecting all the
            criteria for that bundle; endpoints, limit_mask, gt_mask.).
        - WPC: wrong path connections, streamlines connecting correct ROIs but
            not respecting the other criteria for that bundle. The WPC
            statistics are saved into processing_stats.json, but the results
            are only saved if specified in the options. Else, they are merged
            back with the IS.
        - IS: invalid streamlines. All other streamlines.

        Optional:
        - IC: invalid connections, streamlines joining an incorrect combination
            of ROIs. Use carefully, quality depends on the quality of your ROIs
            and no analysis is done on the shape of the streamlines.
        - NC: no connections. Invalid streamlines minus invalid connections.

    In terms of number of voxels:
    - OL : ground truth voxels containing VS streamline(s).
    - OR: voxels containing VS streamline(s) when it shouldn't.

Config file:
    The config file needs to be a json containing a dict of the ground-truth
    bundles as keys. The value for each bundle is itself a dictionnary with:

    Mandatory:
    - endpoints OR [head AND tail]: filename for the endpoints ROI.
        If 'enpoints' is used, we will automatically separate the mask into
        two ROIs, acting as head and tail. Quality check is strongly
        recommended.

    Optional:
        Concerning metrics:
        - gt_mask: expected result. OL and OR metrics will be computed from
            this.*

        Concerning inclusion criteria (other streamlines will be WPC):
        - limits_mask: ROI serving as "all include" criteria of the streamlines.
            To be included in the bundle, streamlines must be entirely included
            in this mask.*
        - angle: angle criteria. Streamlines containing loops and sharp turns
            above given angle will be rejected from the bundle.
        - length: maximum and minimum lengths per bundle.
        - length_x: maximum and mimimum total distance in the x direction (i.e.
            first coordinate).
        - length_y: maximum and mimimum total distance in the y direction (i.e.
            second coordinate).
        - length_z: maximum and mimimum total distance in the z direction (i.e.
            third coordinate).

* Files must be .tck, .trk, .nii or .nii.gz. If it is a tractogram, a mask will
be created. If it is a nifti file, it will be considered to be a mask.

Ex 1:
{
  "Ground_truth_bundle_0": {
    "gt_mask": "PATH/bundle0.nii.gz",
    "angle": 300,
    "length": [140, 150],
    "endpoints": "PATH/file1.nii.gz"
  }
}

Ex 2:
(with options --gt_masks_dir PATH1 --rois_dir PATH2 --limits_mask_dir PATH3)
{
  "Ground_truth_bundle_1": {
    "gt_mask": "bundle1.trk"
    "head": 'file2',
    "tail": 'file3',
    "limits_mask": "general_envelope_bundle_1.nii.gz"
  }
}

"""

import argparse
import json
import itertools
import logging
import numpy as np
import os

from dipy.io.utils import is_header_compatible
from dipy.io.streamline import save_tractogram

from scilpy.io.streamlines import load_tractogram_with_reference
from scilpy.io.utils import (add_overwrite_arg,
                             add_json_args,
                             add_reference_arg,
                             add_verbose_arg,
                             assert_inputs_exist,
                             assert_output_dirs_exist_and_empty)
from scilpy.tractanalysis.reproducibility_measures import compute_dice_voxel
from scilpy.tractanalysis.scoring import (compute_masks,
                                          extract_false_connections,
                                          get_binary_maps,
                                          compute_endpoint_masks,
                                          make_sft_from_ids,
                                          extract_vb_vs)
from scilpy.utils.filenames import split_name_with_nii


def _build_arg_parser():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawTextHelpFormatter)

    p.add_argument("in_tractogram",
                   help="Input tractogram to score")
    p.add_argument("gt_config",
                   help=".json dict configured as specified above.")
    p.add_argument("out_dir",
                   help="Output directory.")

    g = p.add_argument_group("Additions to gt_config")
    g.add_argument("--gt_masks_dir", default='', metavar="DIR",
                   help="Path of the gt_masks listed in the gt_config.\n"
                        "If not set, filenames in the config file are "
                        "considered\n as complete paths.")
    g.add_argument("--limits_masks_dir", default='', metavar="DIR",
                   help="Path of the limits_masks listed in the gt_config.\n"
                        "If not set, filenames in the config file are "
                        "considered\n as complete paths.")
    g.add_argument("--rois_dir", default='', metavar="DIR",
                   help="Path of the ROI files listed in the gt_config\n"
                        "(head + tail or endpoints). If not set, filenames in "
                        "the\nconfig file are considered as complete paths.")
    g.add_argument("--use_gt_masks_as_limits_masks", action='store_true',
                   help="If set, the gt_config's 'gt_mask' will also be used "
                        "as\n'limits_mask' for each bundle. Note that this "
                        "means the\nOR will necessarily be 0.")
    g.add_argument("--use_abs", action='store_true',
                   help="If set, computation of length per orientation\n"
                        "(corresponding to length_x, length_y, length_z in "
                        "the\nconfig file) will use the total of distances in "
                        "absolute\nvalue (ex, coming back on yourself will "
                        "contribute to the\ntotal distance instead of "
                        "cancelling it).")

    g = p.add_argument_group("Preprocessing")
    g.add_argument("--dilate_endpoints",
                   metavar="NB_PASS", default=0, type=int,
                   help="Dilate inclusion masks n-times. Default: 0.")
    g.add_argument("--remove_invalid", action="store_true",
                   help="Remove invalid streamlines before scoring.")

    g = p.add_argument_group("Tractometry choices")
    g.add_argument("--save_wpc_separately", action='store_true',
                   help="If set, streamlines rejected from VC based on the "
                        "config\nfile criteria will be saved separately from "
                        "IS (and IC)\nin one file *_WPC.tck per bundle.")
    g.add_argument("--compute_IC", action='store_true',
                   help="If set, IS are split into NC + IC, where IC are "
                        "computed as one bundle per\npair of ROI not "
                        "belonging to a true connection, named\n*_*_IC.tck.")
    g.add_argument("--remove_wpc_belonging_to_another_bundle",
                   action='store_true',
                   help="If set, WPC actually belonging to VC (from another "
                        "bundle,\nof course; in the case of overlapping ROIs) "
                        "will be removed\nfrom the WPC classification.")

    p.add_argument("--no_empty", action='store_true',
                   help='Do not write file if there is no streamline.')

    add_json_args(p)
    add_overwrite_arg(p)
    add_reference_arg(p)
    add_verbose_arg(p)

    return p


def extract_prefix(filename):
    prefix = os.path.basename(filename)
    prefix, _ = split_name_with_nii(prefix)

    return prefix


def _verify_compatibility_with_bundles(sft, masks_files, parser, args):
    """
    Verifies the compatibility of the main sft with the bundle masks, which can
    be either tractograms or nifti files.
    """
    for file in masks_files:
        if file is not None:
            _, ext = os.path.splitext(file)
            if ext in ['.trk', '.tck']:
                mask = load_tractogram_with_reference(parser, args, file,
                                                      bbox_check=False)
            else:
                mask = file
            compatible = is_header_compatible(sft, mask)
            if not compatible:
                parser.error("Input tractogram incompatible with {}"
                             .format(file))


def load_and_verify_everything(parser, args):
    """
    - Reads the config file
    - Loads the masks / sft
        - If endpoints were given instead of head + tail, separate into two
          sub-rois.
    - Verifies compatibility
    """
    assert_inputs_exist(parser, args.gt_config)
    assert_output_dirs_exist_and_empty(parser, args, args.out_dir,
                                       create_dir=True)
    os.makedirs(os.path.join(args.out_dir, 'segmented_VB'))
    if args.compute_IC:
        os.makedirs(os.path.join(args.out_dir, 'segmented_IB'))
    if args.save_wpc_separately:
        os.makedirs(os.path.join(args.out_dir, 'segmented_WPC'))

    # Read the config file
    (bundle_names, gt_masks_files, limits_masks_files, roi_options,
     lengths, angles, orientation_lengths) = read_config_file(args)

    # Find all masks to be loaded.
    all_mask_files = list(itertools.chain(
        *[list(roi_option.values()) for roi_option in roi_options]))
    all_mask_files = list(dict.fromkeys(all_mask_files))  # Removes duplicates

    # Verify options
    assert_inputs_exist(parser, all_mask_files + [args.in_tractogram],
                        gt_masks_files + limits_masks_files)

    if args.verbose:
        logging.basicConfig(level=logging.INFO)

    logging.info("Loading tractogram.")
    _, ext = os.path.splitext(args.in_tractogram)
    sft = load_tractogram_with_reference(
        parser, args, args.in_tractogram, bbox_check=False)

    if args.remove_invalid:
        sft.remove_invalid_streamlines()

    logging.info("Verifying compatibility of tractogram with gt_masks and "
                 "limits_masks.")
    all_masks = gt_masks_files + limits_masks_files
    all_masks = list(dict.fromkeys(all_masks))  # Removes duplicates
    _verify_compatibility_with_bundles(sft, all_masks, parser, args)

    logging.info("Loading and/or computing ground-truth masks and limits "
                 "masks.")
    gt_masks, _, affine, dimensions, = \
        compute_masks(gt_masks_files, parser, args)
    limits_masks, limits_inv_masks, _, _, = \
        compute_masks(limits_masks_files, parser, args)

    logging.info("Extracting ground-truth head and tail masks.")
    gt_tails, gt_heads = compute_endpoint_masks(
        roi_options, affine, dimensions, args.out_dir)

    # Update all_rois, remove duplicates
    all_rois = gt_tails + gt_heads
    all_rois = list(dict.fromkeys(all_rois))  # Removes duplicates

    logging.info("Verifying tractogram compatibility with endpoint ROIs.")
    for file in all_rois:
        compatible = is_header_compatible(sft, file)
        if not compatible:
            parser.error("Input tractogram incompatible with {}".format(file))

    return (gt_tails, gt_heads, sft, bundle_names, all_rois,
            lengths, angles, orientation_lengths,
            limits_inv_masks, gt_masks, dimensions, ext)


def read_config_file(args):
    """
    Read the gt_config file and returns:

    Returns
    -------
    bundles: List
        The names of each bundle.
    gt_masks: List
        The gt_mask filenames per bundle (None if not set) (used for
        tractometry statistics)
    limits_masks: List
        The limits_masks filenames per bundles (None if not set)
    roi_options: List
        The roi_option dict per bundle. Keys are 'gt_head', 'gt_tail' if
        they are set, else 'gt_endpoints'.
    angles: List
        The maximum angles per bundle (None if not set)
    lengths: List
        The [min max] lengths per bundle (None if not set)
    orientation_length: List
        The [[min_x, max_x], [min_y, max_y], [min_z, max_z]] per bundle.
        (None they are all not set).
    """
    angles = []
    lengths = []
    orientation_lengths = []
    gt_masks = []
    limits_masks = []
    roi_options = []

    with open(args.gt_config, "r") as json_file:
        config = json.load(json_file)

        bundles = list(config.keys())
        for bundle in bundles:
            bundle_config = config[bundle]

            if 'gt_mask' not in bundle_config:
                logging.warning(
                    "No gt_mask set for bundle {}. Some tractometry metrics "
                    "won't be computed (OR, OL).".format(bundle))
            if 'endpoints' not in bundle_config and \
                    'head' not in bundle_config:
                raise ValueError(
                    "Bundle configuration for bundle {} misses 'endpoints' or "
                    "'head'/'tail'".format(bundle))

            angle = length = None
            length_x = length_y = length_z = None
            gt_mask = limit_mask = roi_option = None

            for key in bundle_config.keys():
                if key == 'angle':
                    angle = bundle_config['angle']
                elif key == 'length':
                    length = bundle_config['length']
                elif key == 'length_x':
                    length_x = bundle_config['length_x']
                elif key == 'length_y':
                    length_y = bundle_config['length_y']
                elif key == 'length_z':
                    length_z = bundle_config['length_z']
                elif key == 'gt_mask':
                    gt_mask = os.path.join(args.gt_masks_dir,
                                           bundle_config['gt_mask'])
                    if args.use_gt_masks_as_limits_masks:
                        limit_mask = gt_mask
                elif key == 'limits_mask':
                    if args.use_gt_masks_as_limits_masks:
                        raise ValueError(
                            "With the option --use_gt_masks_as_limits_masks, "
                            "you should not add any limits_mask in the config "
                            "file.")
                    limit_mask = os.path.join(args.limits_masks_dir,
                                              bundle_config['limits_mask'])
                elif key == 'endpoints':
                    if 'head' in bundle_config or 'tail' in bundle_config:
                        raise ValueError(
                            "Bundle {} has confusing keywords in the config "
                            "file. Please choose either endpoints OR "
                            "head/tail.".format(bundle))
                    endpoints = os.path.join(args.rois_dir,
                                             bundle_config['endpoints'])
                    roi_option = {'gt_endpoints': endpoints}
                elif key == 'head':
                    if 'tail' not in bundle_config:
                        raise ValueError(
                            "You have provided the head for bundle {}, but "
                            "not the tail".format(bundle))
                    head = os.path.join(args.rois_dir, bundle_config['head'])
                    tail = os.path.join(args.rois_dir, bundle_config['tail'])
                    roi_option = {'gt_head': head, 'gt_tail': tail}
                elif key == 'tail':
                    pass  # dealt with at head
                else:
                    raise ValueError("Unrecognized value {} in the config "
                                     "file for bundle {}".format(key, bundle))

            angles.append(angle)
            lengths.append(length)
            if length_x is None and length_y is None and length_z is None:
                orientation_lengths.append(None)
            else:
                orientation_lengths.append(
                    [length_x if length_x is not None else [0, np.inf],
                     length_y if length_y is not None else [0, np.inf],
                     length_z if length_z is not None else [0, np.inf]])
            gt_masks.append(gt_mask)
            limits_masks.append(limit_mask)
            roi_options.append(roi_option)

    return (bundles, gt_masks, limits_masks, roi_options,
            lengths, angles, orientation_lengths)


def compute_vb_vs_all_bundles(gt_tails, gt_heads, sft, bundle_names, lengths,
                              angles, orientation_lengths, limits_inv_masks, args,
                              ext):
    """
    Loop on all bundles and extract VS and WPC. Saves the VC but WPC will only
    be saved later if asked by user. Else, they will be included back into IS.

    VS:
       1) Connect the head and tail
       2) Are completely included in the limits_mask (if any)
       3) Have acceptable angle, length and length per orientation.
     +
    WPC connections:
       1) connect the head and tail but criteria 2 and 3 are not respected
    """
    nb_bundles = len(bundle_names)

    vb_sft_list = []
    vs_ids_list = []
    wpc_ids_list = []
    bundles_stats = []
    for i in range(nb_bundles):
        head_filename = gt_heads[i]
        tail_filename = gt_tails[i]

        # Extract true connection
        vs_ids, wpc_ids, bundle_stats = \
            extract_vb_vs(
                sft, head_filename, tail_filename, lengths[i], angles[i],
                orientation_lengths[i], limits_inv_masks[i],
                args.dilate_endpoints, args.use_abs)

        vb_sft = make_sft_from_ids(vs_ids, sft)

        # Save results
        if len(vb_sft) > 0 or not args.no_empty:
            filename = "segmented_VB/{}_VS{}".format(bundle_names[i], ext)
            save_tractogram(vb_sft, os.path.join(args.out_dir, filename),
                            bbox_valid_check=False)

        vb_sft_list.append(vb_sft)
        vs_ids_list.append(vs_ids)
        wpc_ids_list.append(wpc_ids)
        bundles_stats.append(bundle_stats)

        logging.info("Bundle {}: nb VS = {}"
                     .format(bundle_names[i], bundle_stats["VS"]))

    # Duplicates?
    for i in range(nb_bundles):
        for j in range(i + 1, nb_bundles):
            duplicate_ids = np.intersect1d(vs_ids_list[i], vs_ids_list[j])
            if len(duplicate_ids) > 0:
                logging.warning(
                    "{} streamlines belong to true connections of both "
                    "bundles {} and {}.\n"
                    "Please verify your criteria!"
                    .format(len(duplicate_ids), bundle_names[i],
                            bundle_names[j]))

                # Duplicates directory only created if at least one duplicate
                # is found.
                path_duplicates = os.path.join(args.out_dir,
                                               'segmented_conflicts')
                if not os.path.isdir(path_duplicates):
                    os.makedirs(path_duplicates)

                save_tractogram(sft[duplicate_ids], os.path.join(
                    path_duplicates, 'duplicates_' + bundle_names[i] + '_' +
                                     bundle_names[j] + '.trk'))

    return vb_sft_list, vs_ids_list, wpc_ids_list, bundles_stats


def save_wpc_all_bundles(wpc_ids_list, sft, bundles_names, args, ext,
                         vs_ids_list, bundles_stats):
    """
    Cleans WPC (Possibly remove WPC belonging to another bundle) and saves them.
    """
    nb_bundles = len(wpc_ids_list)
    wpc_sft_list = []
    for i in range(nb_bundles):
        wpc_ids = wpc_ids_list[i]

        if args.remove_wpc_belonging_to_another_bundle:
            all_other_gt = list(itertools.chain(
                *[vs_ids_list[j] for j in range(nb_bundles) if j != i]))
            new_wpc_ids = np.setdiff1d(wpc_ids, all_other_gt)
            nb_rejected = len(wpc_ids) - len(new_wpc_ids)
            bundles_stats[i].update(
                {"Belonging to another bundle": nb_rejected})
            wpc_ids = new_wpc_ids

        wpc_sft = make_sft_from_ids(wpc_ids, sft)
        wpc_sft_list.append(wpc_sft)

        if len(wpc_ids) > 0 or not args.no_empty:
            filename = "segmented_WPC/{}_wpc{}".format(bundles_names[i], ext)
            save_tractogram(wpc_sft, os.path.join(args.out_dir, filename),
                            bbox_valid_check=False)
        bundles_stats[i].update({"Cleaned WPC": len(wpc_ids)})

        logging.info("Bundle {}: nb WPC = {}"
                     .format(bundles_names[i], len(wpc_ids)))

    return wpc_sft_list, bundles_stats


def compute_ib_ic_all_bundles(comb_filename, sft, args):
    """
    Loop on all bundles and compute false connections, defined as connections
    between ROIs pairs that do not form gt bundles.

    (Goes through all the possible combinations of endpoints masks)
    """
    ib_sft_list = []
    ic_ids_list = []
    for i, roi in enumerate(comb_filename):
        roi1_filename, roi2_filename = roi

        # Automatically generate filename for Q/C
        prefix_1 = extract_prefix(roi1_filename)
        prefix_2 = extract_prefix(roi2_filename)
        _, ext = os.path.splitext(args.in_tractogram)

        ib_sft, ic_ids = extract_false_connections(
            sft, roi1_filename, roi2_filename, args.dilate_endpoints)

        if len(ib_sft) > 0 or not args.no_empty:
            file = "segmented_IB/{}_{}_IC{}".format(prefix_1, prefix_2, ext)
            save_tractogram(ib_sft, os.path.join(args.out_dir, file),
                            bbox_valid_check=False)

        if len(ib_sft.streamlines) > 0:
            logging.info("IB: Recognized {} streamlines between {} and {}"
                         .format(len(ib_sft.streamlines), prefix_1, prefix_2))

        ib_sft_list.append(ib_sft)
        ic_ids_list.append(ic_ids)

    # Duplicates?
    nb_pairs = len(ic_ids_list)
    for i in range(nb_pairs):
        for j in range(i + 1, nb_pairs):
            duplicate_ids = np.intersect1d(ic_ids_list[i], ic_ids_list[j])
            if len(duplicate_ids) > 0:
                logging.warning(
                    "{} streamlines are scored twice as invalid connections\n"
                    "(between pair {}\n and between pair {}).\n"
                    "You probably have overlapping ROIs!"
                    .format(len(duplicate_ids), comb_filename[i],
                            comb_filename[j]))

    return ib_sft_list, ic_ids_list


def compute_tractometry(all_vs_ids, all_wpc_ids, all_ic_ids, all_nc_ids,
                        vs_ids_list, wpc_ids_list, ic_ids_list,
                        vb_sft_list, wpc_sft_list, ib_sft_list, sft, args,
                        bundles_names, gt_masks, dimensions, comb_filename):
    """
    Tractometry stats: First in terms of connections (NC, IC, VS, WPC), then
    in terms of volume (OL, OR, Dice score)
    """
    nb_bundles = len(bundles_names)

    # Total number of streamlines for each category
    vs_count = len(all_vs_ids)
    wpc_count = len(all_wpc_ids)
    ic_count = len(all_ic_ids)
    nc_count = len(all_nc_ids)
    total_count = len(sft)

    final_results = {
        "tractogram_filename": str(args.in_tractogram),
        "tractogram_overlap": 0.0,
        "VS": vs_count,
        "WPC": wpc_count,
        "IC": ic_count,
        "NC": nc_count,
        "IS = IC + NC": ic_count + nc_count,
        "VB": len([x for x in vs_ids_list if len(x) > 0]),
        "IB": len([x for x in ic_ids_list if len(x) > 0]),
        "WPC_bundle": len([x for x in wpc_ids_list if len(x) > 0]),
        "VS_ratio": vs_count / total_count,
        "IC_ratio": ic_count / total_count,
        "NC_ratio": nc_count / total_count,
        "IS_ratio": (ic_count + nc_count) / total_count,
        "WPC_ratio": wpc_count / total_count,
        "total_streamlines": total_count,
    }

    # Tractometry stats over volume: OL, OR, Dice score
    mean_overlap = 0.0
    mean_overreach = 0.0

    bundle_wise_dict = {}
    for i in range(nb_bundles):
        current_vb = vb_sft_list[i].streamlines

        # Getting the recovered mask
        current_vb_voxels, current_vb_endpoints_voxels = get_binary_maps(
            current_vb, sft)

        vb_results = {"VS": len(current_vb)}
        wpc_results = {}
        if gt_masks[i] is not None:
            gt_total_nb_voxels = np.count_nonzero(gt_masks[i])

            # Dice
            dice = compute_dice_voxel(gt_masks[i], current_vb_voxels)[0]

            # Overlap and overreach
            overlap_mask = gt_masks[i] * current_vb_voxels
            overreach_mask = np.zeros(dimensions)
            overreach_mask[np.where(
                (gt_masks[i] == 0) & (current_vb_voxels >= 1))] = 1

            bundle_lacking = np.zeros(dimensions)
            bundle_lacking[np.where(
                (gt_masks[i] == 1) & (current_vb_voxels == 0))] = 1

            vs_overlap = np.count_nonzero(overlap_mask)
            vs_overreach = np.count_nonzero(overreach_mask)
            vs_lacking = np.count_nonzero(bundle_lacking)

            # Endpoints coverage
            endpoints_overlap = gt_masks[i] * current_vb_endpoints_voxels
            endpoints_overreach = np.zeros(dimensions)
            endpoints_overreach[np.where(
                (gt_masks[i] == 0) & (current_vb_endpoints_voxels >= 1))] = 1

            vb_results.update({
                "dice": dice,
                "OL": vs_overlap,
                "OR": vs_overreach,
                "lacking": vs_lacking,
                "OL_PCT": vs_overlap / gt_total_nb_voxels,
                "OR_PCT": vs_overreach / gt_total_nb_voxels,
                "endpoints_OL": np.count_nonzero(endpoints_overlap),
                "endpoints_OR": np.count_nonzero(endpoints_overreach)
            })

            # WPC
            if args.save_wpc_separately:
                current_wpc_streamlines = wpc_sft_list[i].streamlines
                current_wpc_voxels, _ = get_binary_maps(
                    current_wpc_streamlines, sft)

                # We could add an option to include wpc streamlines to the
                # overreach count. But it seams more natural to exclude wpc
                # streamlines from any count. Separating into a different
                # statistic dict.
                bundle_wpc_overlap = gt_masks[i] * current_wpc_voxels
                bundle_wpc_overreach = np.zeros(dimensions)
                bundle_wpc_overreach[np.where(
                    (gt_masks[i] == 0) & (current_wpc_voxels >= 1))] = 1

                wpc_overlap = np.count_nonzero(bundle_wpc_overlap)
                wpc_overreach = np.count_nonzero(bundle_wpc_overreach)

                wpc_results = {
                    "Count": len(current_wpc_streamlines),
                    "OR": wpc_overreach,
                    "OL": wpc_overlap,
                    "OR_PCT": wpc_overreach / gt_total_nb_voxels,
                    "OL_PCT": wpc_overlap / gt_total_nb_voxels
                }
        bundle_results = {"VB": vb_results}
        if args.save_wpc_separately:
            bundle_results.update({"WPC": wpc_results})

        mean_overlap += vb_results["OL_PCT"]
        mean_overreach += vb_results["OR_PCT"]
        bundle_wise_dict.update({bundles_names[i]: bundle_results})

    if args.compute_IC:
        # -----------
        # False connections stats: number of voxels
        # -----------
        ic_results = {}
        for i, filename in enumerate(comb_filename):
            current_ib = ib_sft_list[i].streamlines

            if len(current_ib):
                current_ib_voxels, _ = get_binary_maps(current_ib, sft)

                bundle_results = {
                    "filename": filename,
                    "IC": len(current_ib),
                    "nb_voxels": np.count_nonzero(current_ib_voxels)
                }
                ic_results.update({str(filename): bundle_results})

        bundle_wise_dict.update({"IB": ic_results})

    final_results.update({
        "bundle_wise": bundle_wise_dict,
        "mean_OL": mean_overlap / nb_bundles,
        "mean_OR": mean_overreach / nb_bundles,
    })

    return final_results


def main():
    parser = _build_arg_parser()
    args = parser.parse_args()

    # Load
    (gt_tails, gt_heads, sft, bundle_names, all_rois, lengths, angles,
     orientation_lengths, limits_inv_masks, gt_masks, dimensions,
     ext) = load_and_verify_everything(parser, args)

    # VS
    logging.info("Scoring valid connections")
    vb_sft_list, vs_ids_list, wpc_ids_list, bundles_stats = \
        compute_vb_vs_all_bundles(gt_tails, gt_heads, sft, bundle_names,
                                  lengths, angles, orientation_lengths,
                                  limits_inv_masks, args, ext)

    # WPC
    if args.save_wpc_separately:
        logging.info("Verifying wpc")
        wpc_sft_list, bundles_stats = save_wpc_all_bundles(
            wpc_ids_list, sft, bundle_names, args, ext, vs_ids_list,
            bundles_stats)
    else:
        wpc_sft_list = []
        wpc_ids_list = []

    # Save bundle stats
    bundle_stats_dict = {}
    for i in range(len(bundle_names)):
        bundle_stats_dict.update({
            bundle_names[i] : bundles_stats[i]
        })
    with open(os.path.join(args.out_dir, "processing_stats.json"), "w") as f:
        json.dump(bundle_stats_dict, f, indent=args.indent,
                  sort_keys=args.sort_keys)

    # IC
    if args.compute_IC:
        logging.info("Scoring invalid connections")

        # Keep all possible combinations
        all_rois = sorted(all_rois)
        comb_filename = list(itertools.combinations(all_rois, r=2))

        # Remove the true connections from all combinations, leaving only
        # false connections
        vb_roi_filenames = list(zip(gt_tails, gt_heads))
        for vb_roi_pair in vb_roi_filenames:
            vb_roi_pair = tuple(sorted(vb_roi_pair))
            comb_filename.remove(vb_roi_pair)
        ib_sft_list, ic_ids_list = compute_ib_ic_all_bundles(comb_filename,
                                                             sft, args)
    else:
        ic_ids_list = []
        ib_sft_list = []
        comb_filename = None

    all_vs_ids = np.unique(list(itertools.chain(*vs_ids_list)))
    all_wpc_ids = np.unique(list(itertools.chain(*wpc_ids_list)))
    all_ic_ids = np.unique(list(itertools.chain(*ic_ids_list)))

    # NC
    # = ids that are not VS, not wpc (if asked) and not IC (if asked).
    all_nc_ids = np.arange(len(sft))
    all_nc_ids = np.setdiff1d(all_nc_ids, all_vs_ids)
    all_nc_ids = np.setdiff1d(all_nc_ids, all_wpc_ids)
    all_nc_ids = np.setdiff1d(all_nc_ids, all_ic_ids)

    if args.compute_IC:
        logging.info("The remaining {} / {} streamlines will be scored as NC."
                     .format(len(all_nc_ids), len(sft)))
        filename = "nc{}".format(ext)
    else:
        logging.info("The remaining {} / {} streamlines will be scored as IS."
                     .format(len(all_nc_ids), len(sft)))
        filename = "is{}".format(ext)

    nc_sft = make_sft_from_ids(all_nc_ids, sft)
    if len(nc_sft) > 0 or not args.no_empty:
        save_tractogram(nc_sft, os.path.join(
            args.out_dir, filename), bbox_valid_check=False)

    # Tractometry
    final_results = compute_tractometry(
        all_vs_ids, all_wpc_ids, all_ic_ids, all_nc_ids, vs_ids_list,
        wpc_ids_list, ic_ids_list, vb_sft_list, wpc_sft_list, ib_sft_list, sft,
        args, bundle_names, gt_masks, dimensions, comb_filename)
    logging.info("Final results saved in {}".format(args.out_dir))
    with open(os.path.join(args.out_dir, "results.json"), "w") as f:
        json.dump(final_results, f, indent=args.indent,
                  sort_keys=args.sort_keys)


if __name__ == "__main__":
    main()
