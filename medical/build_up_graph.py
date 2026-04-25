import os
import json
from typing import Any
from neo4j import GraphDatabase
from tqdm import tqdm
import argparse


# 导入普通实体（批量导入优化）
def import_entity(driver, label, entities):
    print(f'正在批量导入 {label} 类实体数据...')
    query = f"UNWIND $names AS name MERGE (n:{label} {{名称: name}})"
    with driver.session() as session:
        session.run(query, names=entities)


# 导入疾病类实体（带详细属性）
def import_disease_data(driver, entities):
    print(f'正在导入 疾病 类详细数据...')
    query = """
    UNWIND $data AS d
    MERGE (n:疾病 {名称: d.名称})
    SET n.疾病简介 = d.疾病简介,
        n.疾病病因 = d.疾病病因,
        n.预防措施 = d.预防措施,
        n.治疗周期 = d.治疗周期,
        n.治愈概率 = d.治愈概率,
        n.疾病易感人群 = d.疾病易感人群
    """
    with driver.session() as session:
        # 由于疾病数据包含字典，我们分批处理以防内存占用过高
        session.run(query, data=entities)


# 导入关系（参数化查询优化）
def create_all_relationship(driver, relationships):
    print("正在建立实体间关系进度...")
    # 注意：Neo4j 关系类型不支持动态参数，所以使用字符串格式化标签
    with driver.session() as session:
        for t1, n1, rel, t2, n2 in tqdm(relationships):
            query = f"MATCH (a:{t1} {{名称: $n1}}), (b:{t2} {{名称: $n2}}) MERGE (a)-[r:{rel}]->(b)"
            session.run(query, n1=n1, n2=n2)
2

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="通过 medical.json 文件创建一个医疗知识图谱")
    parser.add_argument('--website', type=str, default='neo4j+s://your-id.databases.neo4j.io', help='Neo4j 连接地址')
    parser.add_argument('--user', type=str, default='neo4j', help='用户名')
    parser.add_argument('--password', type=str, required=True, help='密码')
    parser.add_argument('--dbname', type=str, default='neo4j', help='数据库名')
    args = parser.parse_args()

    # 初始化官方驱动
    driver = GraphDatabase.driver(args.website, auth=(args.user, args.password))

    # 清理数据库
    is_delete = input('注意: 是否删除 Neo4j 上的所有实体和关系? (y/n): ')
    if is_delete.lower() == 'y':
        with driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")
            print("数据库已清空。")

    # 读取数据
    data_path = './data/medical_new_2.json'
    if not os.path.exists(data_path):
        print(f"错误: 找不到数据文件 {data_path}")
        exit()

    with open(data_path, 'r', encoding='utf-8') as f:
        all_data = [line.strip() for line in f if len(line.strip()) > 5]

    all_entity = {k: [] for k in ["疾病", "药品", "食物", "检查项目", "科目", "疾病症状", "治疗方法", "药品商"]}
    relationship = []

    print("开始解析原始数据...")
    for data_str in tqdm(all_data):
        try:
            # 兼容处理：有些数据结尾可能有逗号
            if data_str.endswith(','): data_str = data_str[:-1]
            data = eval(data_str)

            disease_name = data.get("name", "")
            if not disease_name: continue

            # 提取疾病实体属性
            all_entity["疾病"].append({
                "名称": disease_name,
                "疾病简介": data.get("desc", ""),
                "疾病病因": data.get("cause", ""),
                "预防措施": data.get("prevent", ""),
                "治疗周期": data.get("cure_lasttime", ""),
                "治愈概率": data.get("cured_prob", ""),
                "疾病易感人群": data.get("easy_get", ""),
            })

            # 药品
            drugs = list(set(data.get("common_drug", []) + data.get("recommand_drug", [])))
            all_entity["药品"].extend(drugs)
            relationship.extend([("疾病", disease_name, "疾病使用药品", "药品", d) for d in drugs])

            # 食物
            do_eat = data.get("do_eat", []) + data.get("recommand_eat", [])
            no_eat = data.get("not_eat", [])
            all_entity["食物"].extend(do_eat + no_eat)
            relationship.extend([("疾病", disease_name, "疾病宜吃食物", "食物", f) for f in do_eat])
            relationship.extend([("疾病", disease_name, "疾病忌吃食物", "食物", f) for f in no_eat])

            # 检查 & 科目 & 症状
            check = data.get("check", [])
            all_entity["检查项目"].extend(check)
            relationship.extend([("疾病", disease_name, "疾病所需检查", "检查项目", c) for c in check])

            dep = data.get("cure_department", [])
            if dep:
                all_entity["科目"].extend(dep)
                relationship.append(("疾病", disease_name, "疾病所属科目", "科目", dep[-1]))

            symptoms = [s[:-3] if s.endswith('...') else s for s in data.get("symptom", [])]
            all_entity["疾病症状"].extend(symptoms)
            relationship.extend([("疾病", disease_name, "疾病的症状", "疾病症状", s) for s in symptoms])

            # 治疗方法
            ways = [w[0] if isinstance(w, list) else w for w in data.get("cure_way", [])]
            ways = [w for w in ways if len(str(w)) >= 2]
            all_entity["治疗方法"].extend(ways)
            relationship.extend([("疾病", disease_name, "治疗的方法", "治疗方法", w) for w in ways])

            # 并发症
            acompany = data.get("acompany", [])
            relationship.extend([("疾病", disease_name, "疾病并发疾病", "疾病", d) for d in acompany])

            # 药品商
            for detail in data.get("drug_detail", []):
                parts = detail.split(',')
                if len(parts) == 2:
                    p, d = parts[0], parts[1]
                    all_entity["药品商"].append(d)
                    all_entity["药品"].append(p)
                    relationship.append(('药品商', d, "生产", "药品", p))
        except Exception as e:
            continue

    # 去重处理
    print("正在进行数据去重...")
    relationship = list(set(relationship))
    for k in all_entity:
        if k != "疾病":
            all_entity[k] = list(set(all_entity[k]))

    # 执行导入
    for k in all_entity:
        if k != "疾病":
            import_entity(driver, k, all_entity[k])
        else:
            import_disease_data(driver, all_entity[k])

    create_all_relationship(driver, relationship)

    driver.close()
    print("知识图谱构建完成！")