#
# Copyright 2016 Quantopian, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from warnings import warn

from empyrical import conditional_value_at_risk
import numpy as np
import pandas as pd

from zipline.assets import Asset, Equity, Future
from zipline.utils.input_validation import expect_types
from .utils.enum import enum
from zipline._protocol import BarData  # noqa


# Datasource type should completely determine the other fields of a
# message with its type.
DATASOURCE_TYPE = enum(
    'AS_TRADED_EQUITY',
    'MERGER',
    'SPLIT',
    'DIVIDEND',
    'TRADE',
    'TRANSACTION',
    'ORDER',
    'EMPTY',
    'DONE',
    'CUSTOM',
    'BENCHMARK',
    'COMMISSION',
    'CLOSE_POSITION'
)

# Expected fields/index values for a dividend Series.
DIVIDEND_FIELDS = [
    'declared_date',
    'ex_date',
    'gross_amount',
    'net_amount',
    'pay_date',
    'payment_sid',
    'ratio',
    'sid',
]
# Expected fields/index values for a dividend payment Series.
DIVIDEND_PAYMENT_FIELDS = [
    'id',
    'payment_sid',
    'cash_amount',
    'share_count',
]

TRADING_DAYS_PER_YEAR = 252
CVAR_LOOKBACK_DAYS = TRADING_DAYS_PER_YEAR * 2
CVAR_CUTOFF = 0.05


class Event(object):

    def __init__(self, initial_values=None):
        if initial_values:
            self.__dict__.update(initial_values)

    def keys(self):
        return self.__dict__.keys()

    def __eq__(self, other):
        return hasattr(other, '__dict__') and self.__dict__ == other.__dict__

    def __contains__(self, name):
        return name in self.__dict__

    def __repr__(self):
        return "Event({0})".format(self.__dict__)

    def to_series(self, index=None):
        return pd.Series(self.__dict__, index=index)


def _deprecated_getitem_method(name, attrs):
    """Create a deprecated ``__getitem__`` method that tells users to use
    getattr instead.

    Parameters
    ----------
    name : str
        The name of the object in the warning message.
    attrs : iterable[str]
        The set of allowed attributes.

    Returns
    -------
    __getitem__ : callable[any, str]
        The ``__getitem__`` method to put in the class dict.
    """
    attrs = frozenset(attrs)
    msg = (
        "'{name}[{attr!r}]' is deprecated, please use"
        " '{name}.{attr}' instead"
    )

    def __getitem__(self, key):
        """``__getitem__`` is deprecated, please use attribute access instead.
        """
        warn(msg.format(name=name, attr=key), DeprecationWarning, stacklevel=2)
        if key in attrs:
            return self.__dict__[key]
        raise KeyError(key)

    return __getitem__


class Order(Event):
    # If you are adding new attributes, don't update this set. This method
    # is deprecated to normal attribute access so we don't want to encourage
    # new usages.
    __getitem__ = _deprecated_getitem_method(
        'order', {
            'dt',
            'sid',
            'amount',
            'stop',
            'limit',
            'id',
            'filled',
            'commission',
            'stop_reached',
            'limit_reached',
            'created',
        },
    )


def asset_multiplier(asset):
    return asset.multiplier if isinstance(asset, Future) else 1


class Portfolio(object):

    def __init__(self):
        self.capital_used = 0.0
        self.starting_cash = 0.0
        self.portfolio_value = 0.0
        self.pnl = 0.0
        self.returns = 0.0
        self.cash = 0.0
        self.positions = Positions()
        self.start_date = None
        self.positions_value = 0.0

    def __repr__(self):
        return "Portfolio({0})".format(self.__dict__)

    # If you are adding new attributes, don't update this set. This method
    # is deprecated to normal attribute access so we don't want to encourage
    # new usages.
    __getitem__ = _deprecated_getitem_method(
        'portfolio', {
            'capital_used',
            'starting_cash',
            'portfolio_value',
            'pnl',
            'returns',
            'cash',
            'positions',
            'start_date',
            'positions_value',
        },
    )

    @property
    def current_portfolio_weights(self):
        """
        Compute each asset's weight in the portfolio by calculating its held
        value divided by the total value of all positions.

        Each equity's value is its price times the number of shares held. Each
        futures contract's value is its unit price times number of shares held
        times the multiplier.
        """
        position_values = pd.Series({
            asset: (
                position.last_sale_price *
                position.amount *
                asset_multiplier(asset)
            )
            for asset, position in self.positions.items()
        })
        return position_values / self.portfolio_value


class AlgorithmPortfolio(Portfolio):

    def __init__(self, data_portal, benchmark=None):
        super(AlgorithmPortfolio, self).__init__()
        self.data_portal = data_portal
        self.benchmark = benchmark
        self.current_date = None

    @property
    def expected_shortfall(self):
        """
        Function for computing expected shortfall (also known as CVaR, or
        Conditional Value at Risk) for the portfolio according to the assets
        currently held and their respective weight in the portfolio.

        This function requires a data portal in order to retrieve price
        histories of the assets in the portfolio.
        """
        data_portal = self.data_portal
        current_date = self.current_date

        # If we do not even have a year's worth of data to look back on
        # then the expected shortfall calculation will not be reliable, so
        # just return NaN. If we only have between one and two years of
        # data just use what is available.
        num_days_of_data = data_portal.trading_calendar.session_distance(
            self.start_date, current_date,
        )
        if num_days_of_data < TRADING_DAYS_PER_YEAR:
            return np.NaN
        elif num_days_of_data < CVAR_LOOKBACK_DAYS:
            num_lookback_days = num_days_of_data
        else:
            num_lookback_days = CVAR_LOOKBACK_DAYS

        # Series mapping each asset to its portfolio weight.
        weights = self.current_portfolio_weights

        assets = map(self._asset_for_history_call, weights.index)
        prices = data_portal.get_history_window(
           assets=assets,
           end_dt=current_date,
           bar_count=num_lookback_days,
           frequency='1d',
           field='price',
           data_frequency='daily',
        )
        asset_returns = prices.pct_change()

        return conditional_value_at_risk(
            returns=asset_returns.fillna(0).dot(weights.values),
            cutoff=CVAR_CUTOFF,
        )

    def _asset_for_history_call(self, asset):
        calendar = self.data_portal.trading_calendar
        asset_finder = self.data_portal.asset_finder
        current_date = self.current_date

        if isinstance(asset, Equity):
            num_days_of_data = calendar.session_distance(
                asset.start_date, current_date,
            )
            if num_days_of_data < TRADING_DAYS_PER_YEAR and \
                    self.benchmark is not None:
                asset = self.benchmark
        elif isinstance(asset, Future):
            # Infer the offset of the given future by comparing it to
            # the upcoming closing contract according to our current
            # date.
            oc = asset_finder.get_ordered_contracts(asset.root_symbol)
            offset = oc.offset_of_contract(asset.sid, current_date.value)
            return asset_finder.create_continuous_future(
                root_symbol=asset.root_symbol,
                offset=offset,
                roll_style='volume',
                adjustment='mul',
            )
        return asset


class Account(object):
    '''
    The account object tracks information about the trading account. The
    values are updated as the algorithm runs and its keys remain unchanged.
    If connected to a broker, one can update these values with the trading
    account values as reported by the broker.
    '''

    def __init__(self):
        self.settled_cash = 0.0
        self.accrued_interest = 0.0
        self.buying_power = float('inf')
        self.equity_with_loan = 0.0
        self.total_positions_value = 0.0
        self.total_positions_exposure = 0.0
        self.regt_equity = 0.0
        self.regt_margin = float('inf')
        self.initial_margin_requirement = 0.0
        self.maintenance_margin_requirement = 0.0
        self.available_funds = 0.0
        self.excess_liquidity = 0.0
        self.cushion = 0.0
        self.day_trades_remaining = float('inf')
        self.leverage = 0.0
        self.net_leverage = 0.0
        self.net_liquidation = 0.0

    def __repr__(self):
        return "Account({0})".format(self.__dict__)

    # If you are adding new attributes, don't update this set. This method
    # is deprecated to normal attribute access so we don't want to encourage
    # new usages.
    __getitem__ = _deprecated_getitem_method(
        'account', {
            'settled_cash',
            'accrued_interest',
            'buying_power',
            'equity_with_loan',
            'total_positions_value',
            'total_positions_exposure',
            'regt_equity',
            'regt_margin',
            'initial_margin_requirement',
            'maintenance_margin_requirement',
            'available_funds',
            'excess_liquidity',
            'cushion',
            'day_trades_remaining',
            'leverage',
            'net_leverage',
            'net_liquidation',
        },
    )


class Position(object):
    @expect_types(asset=Asset)
    def __init__(self, asset):
        self.asset = asset
        self.amount = 0
        self.cost_basis = 0.0  # per share
        self.last_sale_price = 0.0
        self.last_sale_date = None

    @property
    def sid(self):
        # for backwards compatibility
        return self.asset

    def __repr__(self):
        return "Position({0})".format(self.__dict__)

    # If you are adding new attributes, don't update this set. This method
    # is deprecated to normal attribute access so we don't want to encourage
    # new usages.
    __getitem__ = _deprecated_getitem_method(
        'position', {
            'sid',
            'amount',
            'cost_basis',
            'last_sale_price',
            'last_sale_date',
        },
    )


# Copied from Position and renamed.  This is used to handle cases where a user
# does something like `context.portfolio.positions[100]` instead of
# `context.portfolio.positions[sid(100)]`.
class _DeprecatedSidLookupPosition(object):
    def __init__(self, sid):
        self.sid = sid
        self.amount = 0
        self.cost_basis = 0.0  # per share
        self.last_sale_price = 0.0
        self.last_sale_date = None

    def __repr__(self):
        return "_DeprecatedSidLookupPosition({0})".format(self.__dict__)

    # If you are adding new attributes, don't update this set. This method
    # is deprecated to normal attribute access so we don't want to encourage
    # new usages.
    __getitem__ = _deprecated_getitem_method(
        'position', {
            'sid',
            'amount',
            'cost_basis',
            'last_sale_price',
            'last_sale_date',
        },
    )


class Positions(dict):
    def __missing__(self, key):
        if isinstance(key, Asset):
            return Position(key)
        elif isinstance(key, int):
            warn("Referencing positions by integer is deprecated."
                 " Use an asset instead.")
        else:
            warn("Position lookup expected a value of type Asset but got {0}"
                 " instead.".format(type(key).__name__))

        return _DeprecatedSidLookupPosition(key)
