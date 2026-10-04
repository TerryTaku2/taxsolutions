"""Generate sample upload files for a fictional transport company.

All names, numbers and exchange rates are invented for testing; the rates are
NOT official RBZ rates.

    python sample_data/make_samples.py
"""

import csv
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook

HERE = Path(__file__).parent


def sales_ledger():
    wb = Workbook()
    ws = wb.active
    ws.title = "Sales Sep 2026"
    ws.append(["Sample Haulage (Pvt) Ltd - Sales ledger"])
    ws.append(["Period: September 2026"])
    ws.append([])
    ws.append(["Date", "Invoice No", "Customer", "Customer VAT No", "Description", "Currency",
               "Amount Incl VAT", "VAT"])
    rows = [
        (date(2026, 9, 2), "INV-1001", "Greenfield Millers", "10012345", "Local freight Harare-Bulawayo", "USD", 2310.00, 309.98),
        (date(2026, 9, 4), "INV-1002", "Delta Agro Supplies", "10023456", "Local haulage maize Chinhoyi", "USD", 1155.00, 154.99),
        (date(2026, 9, 8), "INV-1003", "Lusaka Copper Traders", "", "Cross-border freight Harare-Lusaka", "USD", 4800.00, 0),
        (date(2026, 9, 11), "INV-1004", "Mutare Timber Co", "10034567", "Local freight timber Mutare-Harare", "ZWG", 30800.00, 4133.33),
        (date(2026, 9, 15), "INV-1005", "Greenfield Millers", "10012345", "Local freight Harare-Gweru", "USD", 1732.50, 232.49),
        (date(2026, 9, 18), "INV-1006", "Beira Port Logistics", "", "International freight Harare-Beira", "USD", 3900.00, 0),
        (date(2026, 9, 22), "CN-0007", "Delta Agro Supplies", "10023456", "Credit note - damaged load INV-1002", "USD", -231.00, -31.00),
        (date(2026, 9, 25), "INV-1008", "Sunrise Retail", "10045678", "Local delivery Harare CBD", "ZWG", 11550.00, 1550.00),
        (date(2026, 9, 29), "INV-1009", "Greenfield Millers", "10012345", "Local freight Harare-Masvingo", "USD", 2079.00, 278.99),
    ]
    for r in rows:
        ws.append(list(r))
    ws.append([])
    ws.append(["", "", "", "", "Total", "", "=SUM(G5:G13)", ""])
    wb.save(HERE / "sales_ledger_sep2026.xlsx")


def purchases():
    rows = [
        ["Date", "Tax Invoice No", "Supplier", "Supplier VAT No", "Description", "Currency", "Total", "VAT"],
        ["03/09/2026", "FUEL-5531", "Zuva Fuels Depot", "20011111", "Diesel 2000 litres", "USD", "3,000.00", ""],
        ["05/09/2026", "T-8812", "Tyre World", "20022222", "Truck tyres x6", "USD", "1,848.00", "248.00"],
        ["09/09/2026", "AUTO-77", "Heavy Trucks Zimbabwe", "20033333", "Truck purchase - Volvo FH horse", "USD", "92,400.00", "12,400.00"],
        ["10/09/2026", "BE-2026-118", "ZIMRA Customs", "", "Import VAT bill of entry - spare parts", "USD", "1,550.00", "1,550.00"],
        ["12/09/2026", "R-441", "Meikles Restaurant", "20044444", "Client entertainment", "USD", "231.00", "31.00"],
        ["16/09/2026", "SRV-902", "Fleet Service Centre", "20055555", "Truck service and parts", "ZWG", "15,400.00", "2,066.67"],
        ["19/09/2026", "", "City Hardware", "", "Workshop consumables", "USD", "115.50", ""],
        ["23/09/2026", "TEL-0923", "Econet Wireless", "20066666", "Airtime and data bundles", "USD", "57.75", "7.75"],
        ["26/09/2026", "INS-3310", "Old Mutual Insurance", "20077777", "Goods in transit insurance", "USD", "462.00", "62.00"],
        ["30/09/2026", "RENT-09", "Workington Properties", "20088888", "Depot rent September", "USD", "1,155.00", "155.00"],
    ]
    with open(HERE / "purchases_sep2026.csv", "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


def bank_statement():
    rows = [
        ["CBZ Bank Limited - Account Statement"],
        ["Account: 01234567890 USD"],
        [],
        ["Transaction Date", "Value Date", "Description", "Debit", "Credit", "Balance"],
        ["01-Aug-2026", "01-Aug-2026", "Balance b/f", "", "", "12,500.00"],
        ["04-Aug-2026", "04-Aug-2026", "RTGS IN Greenfield Millers freight", "", "2,310.00", "14,810.00"],
        ["06-Aug-2026", "06-Aug-2026", "POS Zuva Fuels diesel", "1,500.00", "", "13,310.00"],
        ["08-Aug-2026", "08-Aug-2026", "Monthly bank charges", "25.00", "", "13,285.00"],
        ["08-Aug-2026", "08-Aug-2026", "IMTT 2% tax", "0.50", "", "13,284.50"],
        ["12-Aug-2026", "12-Aug-2026", "RTGS IN Lusaka Copper cross-border freight", "", "4,800.00", "18,084.50"],
        ["15-Aug-2026", "15-Aug-2026", "Salaries August", "6,200.00", "", "11,884.50"],
        ["20-Aug-2026", "20-Aug-2026", "NSSA contributions", "310.00", "", "11,574.50"],
        ["25-Aug-2026", "25-Aug-2026", "ZIMRA VAT payment July", "1,120.00", "", "10,454.50"],
        ["28-Aug-2026", "28-Aug-2026", "Tyre World tyres", "924.00", "", "9,530.50"],
        ["31-Aug-2026", "31-Aug-2026", "Interest earned", "", "4.20", "9,534.70"],
        ["31-Aug-2026", "31-Aug-2026", "Closing balance", "", "", "9,534.70"],
    ]
    with open(HERE / "bank_statement_aug2026.csv", "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


def mobile_money():
    rows = [
        ["Date", "Transaction ID", "Type", "Details", "Amount", "Currency", "Balance"],
        ["2026-08-03 09:15:22", "MP260803.0915.A1", "Merchant Payment", "Payment from T. Moyo - parcel delivery", "45.00", "USD", "245.00"],
        ["2026-08-07 14:02:10", "MP260807.1402.B2", "Merchant Payment", "Payment from Chiedza Stores - delivery", "120.00", "USD", "365.00"],
        ["2026-08-11 11:30:00", "BP260811.1130.C3", "Bill Payment", "ZESA prepaid electricity depot", "-80.50", "USD", "284.50"],
        ["2026-08-19 16:45:51", "SM260819.1645.D4", "Send Money", "Transfer to own account", "-200.00", "USD", "84.50"],
        ["2026-08-22 08:05:12", "MP260822.0805.E5", "Merchant Payment", "Payment from R. Ncube - courier", "2300.00", "ZWG", "2300.00"],
    ]
    with open(HERE / "ecocash_aug2026.csv", "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


def exchange_rates():
    rows = [["Date", "Base", "Quote", "Rate", "Source"]]
    d, rate = date(2026, 7, 1), 26.80
    while d <= date(2026, 10, 31):
        if d.weekday() < 5:
            rows.append([d.isoformat(), "USD", "ZWG", f"{rate:.4f}", "SAMPLE - not official"])
            rate += 0.003
        d += timedelta(days=1)
    with open(HERE / "exchange_rates_SAMPLE.csv", "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


if __name__ == "__main__":
    sales_ledger()
    purchases()
    bank_statement()
    mobile_money()
    exchange_rates()
    print("Sample files written to", HERE)
