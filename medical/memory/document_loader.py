"""文档加载和分片服务"""
import os
from typing import Dict, List
from datetime import datetime
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import PyPDFLoader, Docx2txtLoader, UnstructuredExcelLoader
MAX_TEXT_LENGTH=500_000

class DocumentLoader:
    """文档加载和分片服务"""

    def __init__(
        self,
        chunk_size: int = 500,
        chunk_overlap: int = 5,
        is_Episodic: bool = False,
    ):
        """
        通过初始化参数指定分片策略（只有两种记忆类型）。

        :param is_Episodic:
            - False（默认）: 三级读取（语义记忆/说明书类）
            - True: 两级读取（Episodic Memory/病例类）
        """
        self._is_episodic = is_Episodic

        level_1_size = max(1200, chunk_size * 2)
        level_1_overlap = max(240, chunk_overlap * 2)
        level_2_size = max(600, chunk_size)
        level_2_overlap = max(120, chunk_overlap)
        level_3_size = max(300, chunk_size // 2)
        level_3_overlap = max(60, chunk_overlap // 2)

        self._splitter_level_1 = RecursiveCharacterTextSplitter(
            chunk_size=level_1_size,
            chunk_overlap=level_1_overlap,
            add_start_index=True,
            separators=["\n\n", "\n", "。", "！", "？", "，", "、", " ", ""],
        )
        self._splitter_level_2 = RecursiveCharacterTextSplitter(
            chunk_size=level_2_size,
            chunk_overlap=level_2_overlap,
            add_start_index=True,
            separators=["\n\n", "\n", "。", "！", "？", "，", "、", " ", ""],
        )
        self._splitter_level_3 = RecursiveCharacterTextSplitter(
            chunk_size=level_3_size,
            chunk_overlap=level_3_overlap,
            add_start_index=True,
            separators=["\n\n", "\n", "。", "！", "？", "，", "、", " ", ""],
        )

    @staticmethod
    def _build_chunk_id(filename: str, page_number: int, level: int, index: int) -> str:
        return f"{filename}::p{page_number}::l{level}::{index}"

    def _split_page_to_three_levels(
        self,
        text: str,
        base_doc: Dict,
        page_global_chunk_idx: int,
    ) -> List[Dict]:
        if not text:
            return []

        root_chunks: List[Dict] = []
        page_number = int(base_doc.get("page_number", 0))
        filename = base_doc["filename"]

        level_1_docs = self._splitter_level_1.create_documents([text], [base_doc])
        level_1_counter = 0
        level_2_counter = 0
        level_3_counter = 0

        for level_1_doc in level_1_docs:
            level_1_text = (level_1_doc.page_content or "").strip()
            if not level_1_text:
                continue
            level_1_id = self._build_chunk_id(filename, page_number, 1, level_1_counter)
            level_1_counter += 1

            level_1_chunk = {
                **base_doc,
                "text": level_1_text,
                "chunk_id": level_1_id,
                "parent_chunk_id": "",
                "root_chunk_id": level_1_id,
                "chunk_level": 1,
                "chunk_idx": page_global_chunk_idx,
            }
            page_global_chunk_idx += 1
            root_chunks.append(level_1_chunk)

            level_2_docs = self._splitter_level_2.create_documents([level_1_text], [base_doc])
            for level_2_doc in level_2_docs:
                level_2_text = (level_2_doc.page_content or "").strip()
                if not level_2_text:
                    continue
                level_2_id = self._build_chunk_id(filename, page_number, 2, level_2_counter)
                level_2_counter += 1

                level_2_chunk = {
                    **base_doc,
                    "text": level_2_text,
                    "chunk_id": level_2_id,
                    "parent_chunk_id": level_1_id,
                    "root_chunk_id": level_1_id,
                    "chunk_level": 2,
                    "chunk_idx": page_global_chunk_idx,
                }
                page_global_chunk_idx += 1
                root_chunks.append(level_2_chunk)

                level_3_docs = self._splitter_level_3.create_documents([level_2_text], [base_doc])
                for level_3_doc in level_3_docs:
                    level_3_text = (level_3_doc.page_content or "").strip()
                    if not level_3_text:
                        continue
                    level_3_id = self._build_chunk_id(filename, page_number, 3, level_3_counter)
                    level_3_counter += 1
                    root_chunks.append({
                        **base_doc,
                        "text": level_3_text,
                        "chunk_id": level_3_id,
                        "parent_chunk_id": level_2_id,
                        "root_chunk_id": level_1_id,
                        "chunk_level": 3,
                        "chunk_idx": page_global_chunk_idx,
                    })
                    page_global_chunk_idx += 1

        return root_chunks

    def load_Semantic_document(self, file_path: str, filename: str) -> list[dict]:
        """
        加载单个文档并分片
        :param file_path: 文件路径
        :param filename: 文件名
        :return: 分片后的文档列表
        """
        file_lower = filename.lower()

        if file_lower.endswith(".pdf"):
            doc_type = "PDF"
            loader = PyPDFLoader(file_path)
        elif file_lower.endswith((".docx", ".doc")):
            doc_type = "Word"
            loader = Docx2txtLoader(file_path)
        elif file_lower.endswith((".xlsx", ".xls")):
            doc_type = "Excel"
            loader = UnstructuredExcelLoader(file_path)
        else:
            raise ValueError(f"不支持的文件类型: {filename}")

        try:
            raw_docs = loader.load()
            documents = []
            page_global_chunk_idx = 0
            for doc in raw_docs:
                base_doc = {
                    "filename": filename,
                    "file_path": file_path,
                    "file_type": doc_type,
                    "page_number": doc.metadata.get("page", 0),
                    "loaded_at": datetime.now().strftime("%Y%m%d:%H:%M:%S"),
                }
                page_text = (doc.page_content or "").strip()
                
                page_chunks = self._split_page_to_three_levels(
                    text=page_text,
                    base_doc=base_doc,
                    page_global_chunk_idx=page_global_chunk_idx,
                )
                page_global_chunk_idx += len(page_chunks)
                documents.extend(page_chunks)
            return documents
        except Exception as e:
            raise Exception(f"处理文档失败: {str(e)}")

    def load_Episodic_document(self, file_path: str, filename: str) -> list[dict]:
        """
        加载单个文档并分片
        :param file_path: 文件路径
        :param filename: 文件名
        :return: 分片后的文档列表
        """
        file_lower = filename.lower()

        if file_lower.endswith(".pdf"):
            doc_type = "PDF"
            loader = PyPDFLoader(file_path,mode="single")
        elif file_lower.endswith((".docx", ".doc")):
            doc_type = "Word"
            loader = Docx2txtLoader(file_path)
        elif file_lower.endswith((".xlsx", ".xls")):
            doc_type = "Excel"
            loader = UnstructuredExcelLoader(file_path,mode="single")
        else:
            raise ValueError(f"不支持的文件类型: {filename}")

        try:
            raw_docs = loader.load()

            # Episodic_Memory: 期望 single 模式，整份文档合并成一个 Document。
            # 为了健壮性：即使 loader 返回多个，也合并成一个。
            full_text = "\n".join([(d.page_content or "").strip() for d in raw_docs]).strip()
            # --- 异常拦截：检查文本是否过大 ---
            if len(full_text) > MAX_TEXT_LENGTH:
                raise ValueError(f"文档内容过大 ({len(full_text)} 字符)，超过限制 {MAX_TEXT_LENGTH}")
            if not full_text:
                return []

            base_doc = {
                "filename": filename,
                "file_path": file_path,
                "file_type": doc_type,
                "page_number": 0,
                "loaded_at": datetime.now().strftime("%Y%m%d:%H:%M:%S"),
            }

            documents: List[Dict] = []
            chunk_idx = 0

            # level1：整份文件本身
            level_1_id = self._build_chunk_id(filename, 0, 1, 0)
            documents.append({
                **base_doc,
                "text": full_text,
                "chunk_id": level_1_id,
                "parent_chunk_id": "",
                "root_chunk_id": level_1_id,
                "chunk_level": 1,
                "chunk_idx": chunk_idx,
            })
            chunk_idx += 1

            # level2：用更小粒度的 splitter（这里按你的要求用 level_3）切分
            level_2_docs = self._splitter_level_3.create_documents([full_text], [base_doc])
            level_2_counter = 0
            for level_2_doc in level_2_docs:
                level_2_text = (level_2_doc.page_content or "").strip()
                if not level_2_text:
                    continue
                level_2_id = self._build_chunk_id(filename, 0, 2, level_2_counter)
                level_2_counter += 1
                documents.append({
                    **base_doc,
                    "text": level_2_text,
                    "chunk_id": level_2_id,
                    "parent_chunk_id": level_1_id,
                    "root_chunk_id": level_1_id,
                    "chunk_level": 2,
                    "chunk_idx": chunk_idx,
                })
                chunk_idx += 1

            return documents
        except Exception as e:
            raise Exception(f"处理文档失败: {str(e)}")

if __name__ == "__main__":
    loader = DocumentLoader()
    #documents = loader.load_documents_from_folder("/Users/maruiyao/Desktop/study/agent/RAGQnASystem/bingli")
    #print(documents)
    document = loader.load_Semantic_document("/Users/maruiyao/Desktop/study/agent/MRY_MedicalRag/bingli/dxd体检报告.pdf","dxd体检报告.pdf")
    print(document)