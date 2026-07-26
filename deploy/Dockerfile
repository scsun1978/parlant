# PVG 智能客服 PoC 一体化镜像（Parlant server + PVG MCP 代理共用）
# 构建（仓库根为上下文）：docker compose -f deploy/docker-compose.yml build
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TOKENIZERS_PARALLELISM=false \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_EXTRA_INDEX_URL=https://pypi.org/simple

WORKDIR /app

# 先装 CPU 版 torch（避免 CUDA 依赖把镜像拉大数个 GB；源不可达时回退 PyPI）
RUN pip install "torch>=2.8.0" --index-url https://download.pytorch.org/whl/cpu \
    || pip install "torch>=2.8.0"

# 安装 parlant（本地源码）及所需 extras：deepseek（模型+transformers）、rag（jieba/bm25）
# 注意：构建机无法访问 GitHub，将 parlant-client 的 git 直链替换为 PyPI 同版本（3.2.0）
COPY pyproject.toml README.md ./
RUN sed -i 's|parlant-client @ git+https://github.com/emcie-co/parlant-client-python.git@v3.2.0|parlant-client==3.2.0|' pyproject.toml
COPY src ./src
RUN pip install ".[deepseek,chroma,rag]"

COPY scripts ./scripts

# 运行期数据（parlant-data / knowledge/index / HF 模型缓存）由 compose 以 bind mount 提供
