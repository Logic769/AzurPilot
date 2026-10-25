"""生成自建云契约，不读取或改写前端 API 契约。"""

import json
from pathlib import Path

from deploy.cloud_schema import openapi_document


def main():
    path = Path(__file__).resolve().parents[1] / 'docs/modules/infra/cloud-api-v2.openapi.json'
    path.write_text(json.dumps(openapi_document(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
