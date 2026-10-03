#!/usr/bin/env python3
"""Create conservative, reversible image variants for photographed slides."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np


EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff"}


def order_points(points: np.ndarray) -> np.ndarray:
    points = points.astype("float32")
    ordered = np.zeros((4, 2), dtype="float32")
    sums = points.sum(axis=1)
    differences = np.diff(points, axis=1).reshape(-1)
    ordered[0] = points[np.argmin(sums)]
    ordered[2] = points[np.argmax(sums)]
    ordered[1] = points[np.argmin(differences)]
    ordered[3] = points[np.argmax(differences)]
    return ordered


def detect_slide(image: np.ndarray) -> tuple[np.ndarray, list[list[float]] | None, str]:
    height, width = image.shape[:2]
    scale = min(1.0, 1400.0 / max(height, width))
    preview = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(preview, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 40, 140)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    minimum_area = preview.shape[0] * preview.shape[1] * 0.15
    quadrilateral = None
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:30]:
        if cv2.contourArea(contour) < minimum_area:
            continue
        perimeter = cv2.arcLength(contour, True)
        approximation = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(approximation) == 4 and cv2.isContourConvex(approximation):
            quadrilateral = approximation.reshape(4, 2).astype("float32") / scale
            break
    if quadrilateral is None:
        return image.copy(), None, "full_image_fallback"
    points = order_points(quadrilateral)
    top_left, top_right, bottom_right, bottom_left = points
    output_width = int(max(np.linalg.norm(bottom_right - bottom_left), np.linalg.norm(top_right - top_left)))
    output_height = int(max(np.linalg.norm(top_right - bottom_right), np.linalg.norm(top_left - bottom_left)))
    if output_width < 300 or output_height < 200:
        return image.copy(), None, "detected_region_too_small"
    target = np.array(
        [[0, 0], [output_width - 1, 0], [output_width - 1, output_height - 1], [0, output_height - 1]],
        dtype="float32",
    )
    transform = cv2.getPerspectiveTransform(points, target)
    warped = cv2.warpPerspective(image, transform, (output_width, output_height))
    return warped, points.tolist(), "perspective_rectified"


def sharpness(gray: np.ndarray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--upscale", type=float, default=2.0)
    args = parser.parse_args()

    images = sorted(path for path in args.input_dir.iterdir() if path.is_file() and path.suffix.lower() in EXTENSIONS)
    if not images:
        raise SystemExit(f"no supported images found: {args.input_dir}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for source in images:
        data = np.fromfile(str(source), dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if image is None:
            raise SystemExit(f"cannot decode image: {source}")
        rectified, quadrilateral, route = detect_slide(image)
        interpolation = cv2.INTER_LANCZOS4 if args.upscale > 1 else cv2.INTER_AREA
        enlarged = cv2.resize(rectified, None, fx=args.upscale, fy=args.upscale, interpolation=interpolation)
        gray = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
        denoised = cv2.bilateralFilter(clahe, 7, 35, 35)
        blurred = cv2.GaussianBlur(denoised, (0, 0), 1.2)
        detail = cv2.addWeighted(denoised, 1.55, blurred, -0.55, 0)
        _, otsu = cv2.threshold(detail, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        adaptive = cv2.adaptiveThreshold(
            detail, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 9
        )
        proxy_scale = min(1.0, 1600.0 / max(detail.shape[:2]))
        vlm_proxy = cv2.resize(
            detail,
            None,
            fx=proxy_scale,
            fy=proxy_scale,
            interpolation=cv2.INTER_AREA,
        )
        variants = {
            "00_rectified.png": rectified,
            "01_upscaled.png": enlarged,
            "02_clahe.png": clahe,
            "03_detail.png": detail,
            "04_otsu.png": otsu,
            "05_adaptive.png": adaptive,
            "06_vlm_proxy.png": vlm_proxy,
        }
        target_dir = args.output_dir / source.stem
        target_dir.mkdir(exist_ok=True)
        paths = []
        for name, variant in variants.items():
            target = target_dir / name
            success, encoded = cv2.imencode(".png", variant)
            if not success:
                raise SystemExit(f"cannot encode image variant: {target}")
            encoded.tofile(str(target))
            paths.append(str(target))
        records.append(
            {
                "source": str(source),
                "route": route,
                "quadrilateral": quadrilateral,
                "original_size": [int(image.shape[1]), int(image.shape[0])],
                "rectified_size": [int(rectified.shape[1]), int(rectified.shape[0])],
                "gray_sharpness": round(sharpness(gray), 3),
                "detail_sharpness": round(sharpness(detail), 3),
                "variants": paths,
            }
        )
    receipt = {"schema_version": 1, "upscale": args.upscale, "records": records}
    receipt_path = args.output_dir / "preprocess_receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(receipt_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
