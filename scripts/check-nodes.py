"""打印 ComfyUI 关键节点的真实签名，用于核对工作流 JSON 的节点/字段名。"""

import httpx

NODES = [
    "UNETLoader",
    "CLIPLoader",
    "VAELoader",
    "CLIPTextEncode",
    "EmptyFlux2LatentImage",
    "SkipLayerGuidanceDiTSimple",
    "KSampler",
    "VAEDecode",
    "SaveImage",
]


def main() -> None:
    info = httpx.get("http://127.0.0.1:8188/object_info", timeout=60).json()
    for name in NODES:
        node = info.get(name)
        if not node:
            print(f"{name}: 不存在")
            continue
        inputs = node["input"]
        print(f"{name}")
        print(f"  required: {list(inputs.get('required', {}))}")
        print(f"  optional: {list(inputs.get('optional', {}))}")
    clip_types = info["CLIPLoader"]["input"]["required"]["type"][0]
    print(f"CLIPLoader 支持 moss_image: {'moss_image' in clip_types}")


if __name__ == "__main__":
    main()
