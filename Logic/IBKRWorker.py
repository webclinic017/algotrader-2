import time
import threading
import datetime

from Logic.ApiWrapper import IBapi, createContract, createTrailingStopOrder, create_limit_buy_order, createMktSellOrder
from pytz import timezone

from twsapi.ibapi.execution import ExecutionFilter


class IBKRWorker():
    def __init__(self, settings):
        self.trading_session_state=None
        self.app = IBapi()
        self.settings = settings
        self.app.setting = self.settings
        self.stocks_data_from_server = []
        self.positions_open_on_server=[]
        self.last_worker_execution_time=None
        self.api_connected=False

    def run_full_cycle(self): #add counter 5 times - restart
        try:
            connected=self.connect_to_tws()
            if connected:
                self.check_if_holiday()
                successfull_preparation=self.prepare_and_track()
                if not successfull_preparation:
                    return False
                self.process_positions_candidates()
                return True
            else:
                print("Could not connect to TWS ....processing skept..")
                return True

        except Exception as e:
            self.app.disconnect()
            self.app.reset()
            if hasattr(e, 'message'):
                print("Error in IBKR processing : " + str(e.message))
            else:
                print("Error in IBKR processing : " + str(e))

    def close_all_positions_cycle(self):
        try:
            connected=self.connect_to_tws()
            if connected:
                print("Preparing to close all open positions")
                self.check_if_holiday()
                self.update_open_positions()
                for s, p in self.app.openPositions.items():
                    if 'Value' in p.keys():
                        if p["Value"] != 0:
                            print("Closing " + s)
                            contract = createContract(s)
                            order = createMktSellOrder(p['stocks'])
                            self.app.placeOrder(self.app.nextorderId, contract, order)
                            self.app.nextorderId = self.app.nextorderId + 1
                            print("Created a Market Sell order for " + s)

                        else:
                            print("Position " + s + " skept its Value is 0")
                    else:
                        print("Position " + s + " skept it has no Value")
                return True
            else:
                print("Could not connect to TWS ....processing skept..")
                return False
        except Exception as e:
            self.app.disconnect()
            self.app.reset()
            if hasattr(e, 'message'):
                print("Error in closing all positions : " + str(e.message))
            else:
                print("Error in closing all positions : " + str(e))

    def prepare_and_track(self):
        """
Connecting to IBKR API and initiating the connection instance
        :return:
        """
        print("Connecting")
        try:
            print("Begin prepare and connect")
            self.request_current_PnL()
            self.start_tracking_excess_liquidity()
            self.check_todays_executions()
            self.update_open_positions()

            # request open orders
            self.update_open_orders()
            # start tracking candidates
            succeed=self.evaluate_and_track_candidates()
            if not succeed:
                    print('Problem retrieving market data from TWS more than 60 sec')
                    return False
            if self.app.market_data_error:

                print('Market Data is invalid - check the subscription')
                #report_market_data_error(self.settings)
                return True
            self.update_target_price_for_tracked_stocks()
            print("Connected to IBKR and READY")
            print("Connected and ready")


            requiredCushionForOpenPositions = self.get_required_cushion_for_open_positions()
            remainingFunds = float(self.app.sMa)
            self.real_remaining_funds = remainingFunds - requiredCushionForOpenPositions
            self.app.smaWithSafety = self.real_remaining_funds
            if self.settings.USEMARGIN == False:  # if margin not allowed use net liquidation as maximum
                positions_summary = 0
                for k, p in self.app.openPositions.items():
                    positions_summary += p["Value"]
                self.real_remaining_funds = float(self.app.netLiquidation) - float(positions_summary)
                print("Using own cash only " + "(" + str(self.real_remaining_funds) + "), margin dismissed in settings")
            return True

        except Exception as e:
            if hasattr(e, 'message'):
                print("Error in connection and preparation : " + str(e.message))
            else:
                print("Error in connection and preparation : " + str(e))

    def connect_to_tws(self):
        """
Creates the connection - starts listner for events
        """

        self.app.nextorderId = None
        # while not isinstance(self.app.nextorderId, int):
        retries = 0
        print("Restarting connection to IBKR")
        self.app.disconnect()
        #self.app.reset()
        self.app.connect('127.0.0.1', int(self.settings.PORT), 123)

        # Start the socket in a thread
        api_thread = threading.Thread(target=self.run_loop, name='ibkrConnection', daemon=True)
        api_thread.start()

        # Check if the API is connected via orderid

        while True:
            if isinstance(self.app.nextorderId, int):
                print('Successfully connected to API')
                connected=True
                break
            else:
                print('Waiting for connection...attempt:' + str(retries))
                time.sleep(1)
                retries = retries + 1
                if retries > 10:
                    connected = False
                    break
        if not connected:
            self.app.disconnect()
            self.app.reset()
        return connected

    def evaluate_and_track_candidates(self):
        """
Starts tracking the Candidates and adds the statistics
        """
        time.sleep(1) # clearing messages
        stock_names = [o['ticker'] for o in self.stocks_data_from_server]
        print("Requesting data for " +str(len(stock_names)) + " Candidates")

        # stock_names=stock_names[0:80]   #trimming 90 queries to track less than 100
        self.app.CandidatesLiveDataRequests = {}  # reset candidates requests dictionary

        # starting querry
        trackedStockN = 1
        message_number=0
        self.app.market_data_error=False
        for s in stock_names:
            if len(self.app.CandidatesLiveDataRequests)>90:
                time.sleep(0.5)
                print("Requested more than 90 candidates - waiting to be cleared...")
            id = self.app.nextorderId
            print(
                "starting to track: " + str(trackedStockN) + " of " + str(
                    len(stock_names)) + " " + s +
                " traking with Id:" +
                str(id))
            c = createContract(s)
            self.app.CandidatesLiveDataRequests[id]='requested'
            self.app.candidatesLive[id] = {"Stock": s,
                                           "Close": 0,
                                           "Open": 0,
                                           "Bid": 0,
                                           "Ask": 0,
                                           "averagePriceDropP": 0,
                                           "averagePriceSpreadP": 0,
                                           "tipranksRank": 0,
                                           "yahoo_rank":6,
                                           "stock_invest_rank":0,
                                           "LastUpdate": 0}
            self.app.reqMarketDataType(1)
            self.app.reqMktData(id, c, '', False, False, [])
            while len(self.app.CandidatesLiveDataRequests)>15:
                print('---------more than 20 Candidates quied waiting to clean.... last req'+str(self.app.nextorderId))
                time.sleep(1)
            self.app.nextorderId += 1
            trackedStockN += 1
            message_number+=1
            # if message_number % 10==0:
            #     time.sleep(1)
            #     print("Waiting to clear messages buffer")
        counter=0
        while len(self.app.CandidatesLiveDataRequests)>0:
            print("waiting for the last candidate data...."+str(counter))
            print('missing:'+str(next(iter(self.app.CandidatesLiveDataRequests))))
            counter=counter+1
            time.sleep(1)
            if counter>120:
                return False
        have_empty = True


        # while len(self.app.CandidatesLiveDataRequests):
        #     time.sleep(1)
        #     print("Waiting for last requested candidate data (Closed or Open - depending on session state ) :" + str(counter))
        #     # closings = [str(x['Close']) for x in self.app.candidatesLive.values()]
        #     # if '-' in closings:
        #     #     have_empty = True
        #     # else:
        #     #     have_empty = False
        #     counter += 1
        #     if counter>60:
        #         return False
        m=2


        self.add_market_data_to_live_candidates()

        print(str(len(self.app.candidatesLive)) + " Candidates evaluated and started to track")
        self.api_connected=True
        return True

    def process_positions(self):
        """
Processes the positions to identify Profit/Loss
        """
        print("Processing profits")

        for s, p in self.app.openPositions.items():
            if 'Value' in p.keys():
                if p["Value"] != 0:
                    print("Processing " + s)
                    profit = p["UnrealizedPnL"] / p["Value"] * 100
                    print("The profit for " + s + " is " + str(profit) + " %")
                    if profit > float(self.settings.PROFIT):
                        orders = self.app.openOrders
                        if s in orders:
                            print("Order for " + s + "already exist- skipping")
                        elif int(p["stocks"]) < 0:
                            print(
                                "The " + s + " is SHORT position number of stocks is negative: " + p["stocks"])
                        else:
                            print("Profit for: " + s + " is " + str(profit) +
                                                       "Creating a trailing Stop Order to take a Profit")
                            if self.settings.ALLOWSELL:
                                contract = createContract(s)
                                order = createTrailingStopOrder(p["stocks"], self.settings.TRAIL)

                                self.app.placeOrder(self.app.nextorderId, contract, order)
                                self.app.nextorderId = self.app.nextorderId + 1
                                print("Created a Trailing Stop order for " + s + " at level of " +
                                                        str(self.settings.TRAIL) + "%")
                            else:
                                print("Selling disabled in settings - skipping")
                    elif profit < float(self.settings.LOSS):
                        orders = self.app.openOrders
                        if s in orders:
                            print("Order for " + s + "already exist- skipping")
                        else:
                            print("loss for: " + s + " is " + str(profit) +
                                                       "Creating a Market Sell Order to minimize the Loss")
                            if self.settings.ALLOWSELL:
                                contract = createContract(s)
                                order = createMktSellOrder(p['stocks'])
                                self.app.placeOrder(self.app.nextorderId, contract, order)
                                self.app.nextorderId = self.app.nextorderId + 1
                                print("Created a Market Sell order for " + s)
                            else:
                                print("Selling disabled in settings - skipping")
                    elif profit >2 and bool(self.settings.APPLYMAXHOLD) :
                        positions_dict = {}
                        for po in self.positions_open_on_server:
                            positions_dict[po['ticker']] = datetime.datetime.fromisoformat(po['opened'])
                        opened=positions_dict[s]
                        delta = (datetime.datetime.now() - opened).days
                        if delta>int(self.settings.MAXHOLDDAYS):
                            orders = self.app.openOrders
                            if s in orders:
                                print("Order for " + s + "already exist- skipping")
                            else:
                                print(s + " is held for " + str(delta) +
                                                           " days. Creating a Market Sell Order to utilize the funds")
                                if self.settings.ALLOWSELL:
                                    contract = createContract(s)
                                    order = createMktSellOrder(p['stocks'])
                                    self.app.placeOrder(self.app.nextorderId, contract, order)
                                    self.app.nextorderId = self.app.nextorderId + 1
                                    print("Created a Market Sell order for " + s)
                                else:
                                    print("Selling disabled in settings - skipping")
                else:
                    print("Position " + s + " skept its Value is 0")
            else:
                print("Position " + s + " skept it has no Value")

    def evaluate_stock_for_buy(self, s):
        """
Evaluates stock for buying
        :param s:
        """
        ask_price=None
        target_price=None
        tipRank=None
        average_daily_dropP=None
        print("Evaluating " + s + "for a Buy")
        result='evaluating'
        # finding stock in Candidates
        for c in self.app.candidatesLive.values():
            if c["Stock"] == s:
                ask_price = c["Ask"]
                average_daily_dropP = c["averagePriceDropP"]
                target_price = c["target_price"]
                break

        if ask_price == -1:  # market is closed
            print('The market is closed skipping...')
            result='skept'
        elif ask_price < target_price:
            self.buy_the_stock(ask_price, s)
            result='bought'

        else:
            print(
                "The price of :" + str(ask_price) + "was not in range of :" + str(average_daily_dropP) + " % " )
            result='skept'

        return result

    def update_target_price_for_tracked_stocks(self):
        """
Update target price for all tracked stocks
        :return:
        """
        print("Updating target prices for Candidates")
        for c in self.app.candidatesLive.values():
            print("Updating target price for " + c["Stock"])
            close = c["Close"]
            open = c["Open"]
            average_daily_dropP = c["averagePriceDropP"]

            if open != 0:  # market is open
                c["target_price"] = open - open / 100 * average_daily_dropP
                print("Target price for " + str(c["Stock"]) + " updated to " + str(
                    c["target_price"]) + " based on Open price")
            elif close != 0:  # market is closed - figured from day before
                c["target_price"] = close - close / 100 * average_daily_dropP
                print("Target price for " + str(c["Stock"]) + " updated to " + str(
                    c["target_price"]) + " based on Close price")
            else:

                c["target_price"] = 0
                print("Skept target price for " + str(c["Stock"]) + "Closing price missing")
                continue

    def buy_the_stock(self, price, s):
        """
Creates order to buy a stock at specific price
        :param price: price to buy at limit
        :param s: Stocks to buy
        """
        if self.settings.ALLOWBUY==True:
            contract = createContract(s)
            stocksToBuy = int(int(self.settings.BULCKAMOUNT) / price)
            if stocksToBuy > 0:  # very important - check for available trades everywhere!!!

                order = create_limit_buy_order(stocksToBuy, price)
                self.app.placeOrder(self.app.nextorderId, contract, order)

                self.app.nextorderId = self.app.nextorderId + 1
                print(
                    "Issued the BUY order at " + str(price) + "for " + str(stocksToBuy) + " Stocks of " + s)

            else:
                print("The single stock is too expensive - skipping")
        else:
            print("Buying is not allowed in Settings - skipping the buying order")

    def process_candidates(self):
        """
processes candidates for buying if enough SMA
        :return:
        """


        if self.real_remaining_funds < self.settings.BULCKAMOUNT:
            print("SMA (including open positions cushion) is " + str(
                self.real_remaining_funds) + " it is less than 1000 - skipping buy")
            return
        else:
            print(
                "SMA (including open positions cushion) is :" + str(self.real_remaining_funds) + " searching candidates")
            # updating the targets if market was open in the middle
            self.update_target_price_for_tracked_stocks()
            res=self.app.candidatesLive.items()
            # res=sort_by_parameter_desc(self.app.candidatesLive.items(),'twelve_month_momentum')
            # res = sorted(sorted(sorted(sorted(self.app.candidatesLive.items(), key=lambda x: x[1]['twelve_month_momentum'], reverse=False), key=lambda x: x[1]['under_priced_pnt'], reverse=False), key=lambda x: x[1]['yahoo_rank'], reverse=False), key=lambda x: x[1]['tipranksRank'], reverse=True)
            print(str(len(res)) + "Candidates found,sorted by Yahoo ranks")
            for i, c in res:
                if self.app.tradesRemaining > 0 or self.app.tradesRemaining == -1:
                    if c['Stock'] in self.app.openPositions:
                        print("Skipping " + c['Stock'] + " as it is in open positions.")
                        continue
                    elif c['Stock'] in self.app.openOrders.keys():
                        print("Skipping " + c['Stock'] + " as it is in open orders.")
                        continue
                    else:
                        result=self.evaluate_stock_for_buy(c['Stock'])
                        if result=='bought':
                            break                    #to avoid buying more than one in a worker run
                else:
                    print("Skipping " + c['Stock'] + " no available trades.")

    def process_positions_candidates(self):
        """
Process Open positions and Candidates
        """
        try:
            est = timezone('US/Eastern')
            fmt = '%Y-%m-%d %H:%M:%S'
            est_time = datetime.datetime.now(est).strftime(fmt)
            local_time=datetime.datetime.now().strftime(fmt)
            print("----Starting Worker...----EST Time: " + est_time + "----Local Time: "+local_time+"----------")
            print("Processing Positions-Candidates ")
            if self.trading_session_state == "Open":
                # process
                if len(self.app.candidatesLive.items())>0:
                    self.process_candidates()
                self.process_positions()
                self.last_worker_execution_time = datetime.datetime.now()
            else:
                print("Trading session is not Open - processing skept")

            print(
                "...............Worker finished....EST Time: " + est_time + "....Local Time: "+local_time+"........")
            self.app.disconnect()
            #self.app.reset()
        except Exception as e:
            if hasattr(e, 'message'):
                print("Error in processing Worker : " + str(e.message))
            else:
                print("Error in processing Worker : " + str(e))

    def run_loop(self):
        self.app.run()

    def update_open_positions(self):
        """
updating all openPositions, refreshed on each worker- to include changes from new positions after BUY
        """
        # update positions from IBKR
        print("Updating open Positions:")
        self.app.openPositionsLiveDataRequests = {}  # reset requests dictionary as positions could be changed...
        self.app.openPositions = {}  # reset open positions
        self.app.temp_positions = {}

        self.app.finishedPostitionsGeneral = False  # flag to ensure all positions received
        self.app.reqPositions()  # requesting open positions
        time.sleep(0.1)
        counter = 0
        while (self.app.finishedPostitionsGeneral != True):
            time.sleep(1)
            counter += 1
        for s, p in self.app.temp_positions.items():  # start tracking one by one
            id = self.app.nextorderId
            self.app.openPositionsLiveDataRequests[id] = s
            self.app.reqPnLSingle(id, self.settings.ACCOUNT, "", p["conId"])
            print("Requested details for " + s + " position PnL with reqest : "+str(id))
            self.app.nextorderId += 1

            while (len(self.app.openPositionsLiveDataRequests) != 0):
                time.sleep(0.1)
                print('Waiting to get data for position request :'+str(self.app.nextorderId-1))
        print(str(len(self.app.openPositions)) + " open positions completely updated")

    def update_open_orders(self):
        """
Requests all open orders
        """
        print("Updating all open orders")
        self.app.openOrders = {}
        self.app.finishedReceivingOrders = False
        self.app.reqAllOpenOrders()
        while(self.app.finishedReceivingOrders!=True):
            print('Waiting to receive all open orders....')
            time.sleep(1)

        print(str(len(self.app.openOrders)) + " open orders found ")

    def start_tracking_excess_liquidity(self):
        """
Start tracking excess liquidity - the value is updated every 3 minutes
        """
        # todo: add safety to not buy faster than every 3 minutes
        print("Starting to track Excess liquidity")
        id = self.app.nextorderId
        self.app.reqAccountSummary(id, "All", "ExcessLiquidity,DayTradesRemaining,NetLiquidation,SMA")
        self.app.nextorderId += 1

    def request_current_PnL(self):
        """
Creating a PnL request the result will be stored in generalStarus
        """
        global id, status
        id = self.app.nextorderId
        print("Requesting Daily PnL")
        self.app.reqPnL(id, self.settings.ACCOUNT, "")
        # time.sleep(0.5)
        self.app.nextorderId = self.app.nextorderId + 1
        print(self.app.generalStatus)

    def get_required_cushion_for_open_positions(self):
        requiredCushion = 0
        existing_positions = self.app.openPositions
        for k, v in existing_positions.items():
            value = v['Value']
            if v['stocks'] != 0:
                profit = v['UnrealizedPnL']
                clearvalue = value - profit
                canLose = abs(int(self.settings.LOSS))
                requiredcushionForPosition = clearvalue / 100 * canLose
                requiredcushionForPosition += profit
                requiredCushion += requiredcushionForPosition
        return requiredCushion

    def request_ticker_data(self, ticker: str):
        # todo implement ticker data functionality

        contract = createContract(ticker)
        id = self.app.nextorderId
        self.app.contract_processing = True
        self.app.reqContractDetails(self.app.nextorderId, contract)
        self.app.nextorderId = self.app.nextorderId + 1
        while self.app.contract_processing:
            time.sleep(0.1)
        cd = self.app.contractDetailsList[id]
        i = 5
        return cd

    def check_if_holiday(self):
        id = self.app.nextorderId
        c = createContract('AAPL')# checked always with AAPL - can be no candidates
        self.app.reqContractDetails(id, c)
        while (self.app.trading_hours_received != True):
            time.sleep(1)
        session_info_to_parse = self.app.trading_session
        today_string = session_info_to_parse.split(";")[0]
        if 'CLOSED' in today_string:
            self.trading_session_holiday = True
            self.app.trading_session_state = "Closed"
        else:
            self.trading_session_holiday = False
            self.check_session_state()

        self.app.nextorderId += 1

    def add_market_data_to_live_candidates(self):
        for k, v in self.app.candidatesLive.items():
            for dt in self.stocks_data_from_server:
                if dt['ticker'] == v['Stock']:
                    self.app.candidatesLive[k]["averagePriceDropP"] = dt['yahoo_avdropP']
                    self.app.candidatesLive[k]["averagePriceSpreadP"] = dt['yahoo_avspreadP']
                    self.app.candidatesLive[k]['tipranksRank'] = dt['tipranks']
                    self.app.candidatesLive[k]['yahoo_rank'] = dt['yahoo_rank']
                    self.app.candidatesLive[k]['stock_invest_rank'] = dt['stock_invest_rank']
                    self.app.candidatesLive[k]['fmp_rating'] = dt['fmp_rating']
                    self.app.candidatesLive[k]['under_priced_pnt'] = dt['under_priced_pnt']
                    self.app.candidatesLive[k]['twelve_month_momentum'] = dt['twelve_month_momentum']
                    self.app.candidatesLive[k]['beta'] = dt['beta']
                    self.app.candidatesLive[k]['max_intraday_drop_percent'] = dt['max_intraday_drop_percent']
                    print(
                        "Ticker Data from server for " + v['Stock'] + " was added")
                    break


    def check_session_state(self):
        tz = timezone('US/Eastern')
        current_est_time=datetime.datetime.now(tz).time()
        dstart = datetime.time(4, 0, 0)
        dend=datetime.time(20, 0, 0)
        tstart=datetime.time(9, 30, 0)
        tend=datetime.time(16, 0, 0)
        if time_in_range(dstart,tstart,current_est_time):
            self.trading_session_state = "Pre Market"
        elif time_in_range(tstart,tend,current_est_time):
            self.trading_session_state = "Open"
        elif time_in_range(tend,dend,current_est_time):
            self.trading_session_state = "After Market"
        else:
            self.trading_session_state = "Closed"
        self.app.trading_session_state = self.trading_session_state

    def check_todays_executions(self):
        self.app.executions_received=False
        id = self.app.nextorderId
        self.app.reqExecutions(id, ExecutionFilter())
        while (self.app.executions_received != True):
            time.sleep(1)

        self.app.nextorderId += 1


def time_in_range(start, end, x):
    """Return true if x is in the range [start, end]"""
    if start <= end:
        return start <= x <= end
    else:
        return start <= x or x <= end


def sort_by_parameter_desc(object,property):
    return sorted(object, key=lambda x: x[1][property], reverse=False)