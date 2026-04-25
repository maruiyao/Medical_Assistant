import json

def extract_all_names(input_file, output_file):
    names = []
    
    with open(input_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                # 将每一行解析为字典
                data = json.loads(line)
                if "name" in data:
                    names.append(data["name"])
            except json.JSONDecodeError:
                print(f"跳过错误行: {line[:30]}...")

    # 打印结果或保存到文件
    for name in names:
        print(name)
    
    # 选做：保存到文本文件
    with open(output_file, 'w', encoding='utf-8') as f_out:
        for name in names:
            f_out.write(name + '\n')

# 执行
extract_all_names('/Users/maruiyao/Desktop/study/agent/MRY_MedicalRag/data/medical.json', '/Users/maruiyao/Desktop/study/agent/MRY_MedicalRag/data/name_list.txt')